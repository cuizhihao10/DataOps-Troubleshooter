"""基于官方 MCP SDK 的 Streamable HTTP 客户端（`mcp-transport:v1`）。

这是生产接入路径：真实形态下 LTS / BDS / FlashSync 由一个独立部署的 MCP 运维网关代理，网关与
Agent 分属不同部署单元，因此 client↔server 这一跳必须走 HTTP 而不能是本地子进程。模块把令牌、
连接池、超时和错误分类集中在一处，出口仍复用 `extract_payload` + `McpToolResponse` 校验，因此
协议边界外依然不信任服务端数据。

两个刻意取舍写在这里而不是只留在文档里：共享一个长寿命 `httpx.AsyncClient`（省掉每次调用的
TCP/TLS 握手），但**每次调用新建一个 MCP 会话**（保住"调用之间完全隔离"，使 `asyncio.gather`
的并行批次不需要额外并发推理）。详细论证见实现指南 5.2。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from app.domain.tooling import McpToolRequest, McpToolResponse, ToolErrorCode, ToolName
from app.mcp.protocol import (
    McpClientError,
    McpToolDescriptor,
    extract_payload,
    to_tool_descriptors,
)


class StreamableHttpMcpClient:
    """通过 Streamable HTTP 调用远程 MCP 网关的九个只读工具。

    实例持有一个共享连接池与一份固定 `Authorization` 头，每次发现或调用则新建一个 MCP 会话：
    SDK 不关闭调用方提供的 client（`mcp/client/streamable_http.py:637-654` 只在自己创建时才
    `enter_async_context`），所以池化不需要任何 hack。超时、连接失败与 HTTP 状态码都被映射成
    分类 `McpClientError`；401/403 映射为 PERMISSION_DENIED 而不是 SERVICE_UNAVAILABLE，因此
    错令牌只会产生一个 ToolEvent，不会被执行器当成瞬时故障反复重试。
    """

    def __init__(
        self,
        *,
        url: str,
        auth_token: SecretStr | None = None,
        timeout_seconds: float = 5,
    ) -> None:
        """保存网关地址、构造带令牌与超时的共享 httpx 客户端，并校验令牌可安全进入请求头。

        令牌只进 `Authorization` 头、绝不进 URL，避免它出现在网关访问日志、trace 或异常文本里。
        含空白或非 ASCII 的令牌在构造期就拒绝：`\\r\\n` 会变成请求头注入，而 httpx 只会在第一次
        真正调用工具时才报错，那时进程已经对外宣称健康。构造不发起任何网络请求，因此无副作用。
        """

        headers: dict[str, str] = {}
        if auth_token is not None:
            secret = auth_token.get_secret_value()
            if not secret.isascii() or not secret.isprintable() or " " in secret:
                raise ValueError("mcp auth token must be printable ASCII without spaces")
            headers["Authorization"] = f"Bearer {secret}"
        self._url = url
        self._timeout_seconds = timeout_seconds
        # follow_redirects 关掉：网关地址是显式配置的内网地址，任何重定向都意味着配置错了，
        # 而跟随重定向会把 Bearer 令牌送去一个没打算信任的主机。宁可立刻失败也不要静默跟随。
        # trust_env 关掉是同一条理由的另一半：httpx 默认会读环境变量与 Windows 注册表里的系统
        # 代理，那会把网关令牌经一个不在信任边界内的代理转发，还会把"端口无人监听"变成代理侧
        # 读超时，让 SERVICE_UNAVAILABLE 被误分类成 TIMEOUT。到内网网关的这一跳必须直连。
        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        self._closed = False

    async def list_tools(self) -> tuple[str, ...]:
        """通过真实 MCP 工具发现返回按名称排序的不可变工具名列表。

        方法复用完整 descriptor 查询，保证名称与安全注解来自同一次协议响应；tuple 和排序让健康
        快照稳定。发现失败以分类 `McpClientError` 传播，不返回本地枚举冒充成功——否则网关宕机会
        被健康检查读成"九个工具齐全"。
        """

        descriptors = await self.list_tool_descriptors()
        return tuple(descriptor.name for descriptor in descriptors)

    async def list_tool_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        """建立一次 HTTP MCP 会话、执行 `list_tools` 并提取可审计安全元数据。

        总超时包住连接、initialize 和请求全过程，因此网关半开连接不会挂死启动审计；错误分类与
        `call_tool` 完全一致，启动阶段的"401 还是连不上"因此可以直接从错误码读出来。
        """

        async with self._guarded("list_tools"):
            async with self._session() as session:
                result = await session.list_tools()
        return to_tool_descriptors(result.tools)

    async def call_tool(
        self,
        tool_name: ToolName,
        request: McpToolRequest,
    ) -> McpToolResponse:
        """调用一个白名单工具，并将协议载荷校验为统一响应模型。

        `ToolName` 与 `McpToolRequest` 已在进入本方法前完成白名单和字段校验；请求以 JSON 模式
        序列化以正确传输 datetime。会话是本次调用专属的，因此同一批并行 Action 不共享任何可变
        协议状态；收到结果后只接受结构化字典或可解析 JSON 文本，再由 Pydantic 拒绝契约漂移。
        """

        async with self._guarded(f"tool {tool_name.value}"):
            async with self._session() as session:
                result = await session.call_tool(
                    tool_name.value,
                    arguments=request.model_dump(mode="json"),
                    read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                )

        # 传输成功不代表业务契约合法；必须在边界处再次进行 Pydantic 校验。
        payload = extract_payload(result)
        return McpToolResponse.model_validate(payload)

    async def aclose(self) -> None:
        """关闭共享连接池，可重复调用；关闭后的调用返回 SERVICE_UNAVAILABLE 而不是裸异常。

        lifespan 的 finally 会无条件调用它，因此幂等是必需的而不是锦上添花。先置标记再关闭：这样
        即使 `aclose` 自身抛错，后续调用也不会去用一个半关闭的池，而是拿到明确的分类错误。
        """

        if self._closed:
            return
        self._closed = True
        await self._http_client.aclose()

    @asynccontextmanager
    async def _guarded(self, operation: str) -> AsyncIterator[None]:
        """用总超时和统一错误分类包住一次协议往返，把所有失败标准化为 `McpClientError`。

        总超时必须包住会话创建：网关接受了 TCP 连接却不回 initialize 时，单次读取超时尚未生效，
        没有外层预算就会挂到墙钟耗尽。关闭后的调用在这里直接拒绝，而不是等 httpx 抛
        "client has been closed"——那条消息随版本变化，分类会退化成兜底的 SERVICE_UNAVAILABLE。
        """

        if self._closed:
            raise McpClientError(
                f"MCP {operation} failed: transport client is closed",
                ToolErrorCode.SERVICE_UNAVAILABLE,
            )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                yield
        except TimeoutError as exc:
            raise McpClientError(f"MCP {operation} timed out", ToolErrorCode.TIMEOUT) from exc
        except McpClientError:
            raise
        except Exception as exc:
            error_code, message = _classify_transport_failure(exc)
            raise McpClientError(f"MCP {operation} failed: {message}", error_code) from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        """为本次调用建立一个专属 MCP 会话，复用实例共享的 httpx 连接池。

        `http_client` 由调用方提供，SDK 因此不会在退出时关闭它，池得以跨调用存活；会话本身仍是
        一次性的，所以并行批次里的每个 Action 都拥有独立协议状态。`terminate_on_close` 保留默认
        True：网关当前是 stateless 模式、没有 session id，DELETE 不会真的发出，但一旦有人把网关
        改成有状态，这个默认值就是防止服务端会话泄漏的安全网。
        """

        async with streamable_http_client(self._url, http_client=self._http_client) as (
            read_stream,
            write_stream,
            _session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                # initialize 是协议协商而不是"连接可用"的同义词；只有握手成功才把会话交给调用方。
                await session.initialize()
                yield session


def _flatten_exceptions(exc: BaseException) -> Iterator[BaseException]:
    """深度展开异常树，使嵌套 ExceptionGroup 里的真实传输失败也能被分类。

    SDK 的 `post_writer` 用 `tg.start_soon` 派发请求（`mcp/client/streamable_http.py:524-571`），
    因此一个 401 会以 BaseExceptionGroup 的形式冒出 anyio task group，只看最外层类型只能得到
    "某个组失败了"。递归展开而不是只看一层：任务组可以嵌套，扁平化后分类规则才与传输细节解耦。
    """

    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from _flatten_exceptions(nested)


def _classify_transport_failure(exc: BaseException) -> tuple[ToolErrorCode, str]:
    """把 httpx 与 anyio 的失败映射为 ToolErrorCode，并给出不含凭据的可审计消息。

    HTTP 状态码优先于超时：网关在 401 之后关闭连接可能顺带产生读超时，若先看超时就会把"令牌错"
    误判成瞬时故障并重试。401/403 映射为 PERMISSION_DENIED（不在 `RETRYABLE_TOOL_ERRORS` 内），
    其余状态码与传输异常映射为 SERVICE_UNAVAILABLE。消息自行构造而不是复用异常文本，避免请求
    头或响应体细节进入 ToolEvent。
    """

    flattened = tuple(_flatten_exceptions(exc))
    for candidate in flattened:
        if isinstance(candidate, httpx.HTTPStatusError):
            status = candidate.response.status_code
            if status in {401, 403}:
                return (
                    ToolErrorCode.PERMISSION_DENIED,
                    f"MCP gateway rejected the request with HTTP {status}",
                )
            return (
                ToolErrorCode.SERVICE_UNAVAILABLE,
                f"MCP gateway returned HTTP {status}",
            )
    for candidate in flattened:
        if isinstance(candidate, httpx.TimeoutException):
            return (ToolErrorCode.TIMEOUT, "MCP gateway request timed out")
    # 兜底只报异常类型名而不是整段文本：类型足以定位问题（ConnectError / ReadError / ...），又不会把
    # 不可控的第三方消息原样搬进对外可见的 ToolEvent。取第一个叶子异常而不是最外层：最外层往往
    # 就是 anyio 的 ExceptionGroup，报"ExceptionGroup"等于什么都没说。
    leaf = next(
        (item for item in flattened if not isinstance(item, BaseExceptionGroup)),
        exc,
    )
    return (
        ToolErrorCode.SERVICE_UNAVAILABLE,
        f"MCP transport error {type(leaf).__name__}",
    )
