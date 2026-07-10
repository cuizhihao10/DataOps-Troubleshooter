"""基于官方 MCP SDK 的 stdio 客户端适配器。

客户端负责启动独立 FastMCP 子进程、完成 initialize 握手、发现工具注解并解析结构化
返回。所有传输异常都会被转换成带错误分类的 McpClientError，供确定性执行器记录。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict

from app.domain.tooling import McpToolRequest, McpToolResponse, ToolErrorCode, ToolName


class McpClientError(RuntimeError):
    """把 MCP 传输或协议失败映射为执行器可分类处理的领域异常。

    异常保留可读消息和统一 ToolErrorCode，使上层无需依赖 SDK 私有异常类型即可决定是否重试。
    该类型只表示尚未得到合法 `McpToolResponse` 的客户端失败，不用于包装工具返回的业务错误。
    """

    def __init__(self, message: str, error_code: ToolErrorCode) -> None:
        """初始化错误消息与标准错误码，供重试策略和 ToolEvent 记录读取。

        `message` 交给 RuntimeError 保持常规异常行为，`error_code` 单独保存以避免执行器解析文本；
        调用方应限制最终公开消息长度，防止底层传输输出无限扩张。
        """

        super().__init__(message)
        self.error_code = error_code


class McpToolDescriptor(BaseModel):
    """保存 MCP 工具发现阶段需要审计的名称、只读注解和输出 Schema 状态。

    健康检查与集成测试使用该快照验证九个工具确实通过协议公开且保持非破坏性。模型不复制完整
    JSON Schema，以减少启动状态体积；对外执行参数仍由领域 Pydantic 模型严格校验。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    read_only: bool
    destructive: bool
    idempotent: bool
    has_output_schema: bool


class StdioMcpClient:
    """通过官方 MCP SDK 管理一次一会话的本地 stdio 工具调用。

    每个发现或调用操作都会启动隔离服务进程、initialize、执行请求并关闭资源，简单可靠且适合
    小型演示；代价是比长连接多一次进程开销。超时和传输异常被标准化，结构化返回再次经过
    `McpToolResponse` 校验，确保协议边界外仍不信任 Mock 服务数据。
    """

    def __init__(
        self,
        *,
        server_module: str = "mcp_server.server",
        timeout_seconds: float = 5,
        cwd: Path | None = None,
    ) -> None:
        """配置服务模块、单次操作超时和子进程工作目录。

        默认使用当前 Python 解释器运行仓库内 `mcp_server.server`，避免虚拟环境错配；cwd 可由
        测试覆盖以验证不同启动位置。这里只保存配置，不提前启动进程，因此构造客户端无副作用。
        """

        self._server_module = server_module
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd or Path.cwd()

    async def list_tools(self) -> tuple[str, ...]:
        """通过真实 MCP 工具发现返回按名称排序的不可变工具名列表。

        方法复用完整 descriptor 查询，保证名称与安全注解来自同一次协议响应；tuple 和排序让
        健康快照稳定。超时、握手或传输失败会以 `McpClientError` 传播，不返回本地枚举冒充成功。
        """

        descriptors = await self.list_tool_descriptors()
        return tuple(descriptor.name for descriptor in descriptors)

    async def list_tool_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        """建立 stdio 会话、执行 `list_tools` 并提取可审计安全元数据。

        外层 asyncio 超时覆盖进程启动、initialize 和请求全过程；SDK 或子进程异常统一映射为
        SERVICE_UNAVAILABLE，明确的超时映射为 TIMEOUT。成功后把可能缺失的协议注解保守转换为
        False，并按名称排序，调用方可据此拒绝缺少只读声明或输出 Schema 的服务。
        """

        try:
            # 总超时必须包住会话创建，否则服务进程卡在 initialize 时 read timeout 尚未生效。
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session() as session:
                    result = await session.list_tools()
        except TimeoutError as exc:
            raise McpClientError("MCP list_tools timed out", ToolErrorCode.TIMEOUT) from exc
        except McpClientError:
            raise
        except Exception as exc:
            raise McpClientError(
                f"MCP list_tools transport failed: {exc}",
                ToolErrorCode.SERVICE_UNAVAILABLE,
            ) from exc
        # 只抽取安全门禁需要的字段，避免把 SDK 对象泄漏到应用领域层。
        return tuple(
            sorted(
                (
                    McpToolDescriptor(
                        name=tool.name,
                        read_only=bool(tool.annotations and tool.annotations.readOnlyHint),
                        destructive=bool(tool.annotations and tool.annotations.destructiveHint),
                        idempotent=bool(tool.annotations and tool.annotations.idempotentHint),
                        has_output_schema=tool.outputSchema is not None,
                    )
                    for tool in result.tools
                ),
                key=lambda descriptor: descriptor.name,
            )
        )

    async def call_tool(
        self,
        tool_name: ToolName,
        request: McpToolRequest,
    ) -> McpToolResponse:
        """调用一个白名单工具，并将协议载荷校验为统一响应模型。

        `ToolName` 与 `McpToolRequest` 已在进入本方法前完成白名单和字段校验；请求以 JSON 模式
        序列化以正确传输 datetime。总超时和 SDK 读取超时共同限制卡死，客户端失败转换为分类
        异常；收到结果后只接受结构化字典或可解析 JSON 文本，再由 Pydantic 拒绝契约漂移。
        """

        try:
            # 同时限制整个生命周期和单次读取，覆盖子进程启动慢与服务响应慢两类问题。
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session() as session:
                    result = await session.call_tool(
                        tool_name.value,
                        arguments=request.model_dump(mode="json"),
                        read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                    )
        except TimeoutError as exc:
            raise McpClientError(
                f"MCP tool {tool_name.value} timed out",
                ToolErrorCode.TIMEOUT,
            ) from exc
        except McpClientError:
            raise
        except Exception as exc:
            raise McpClientError(
                f"MCP tool {tool_name.value} transport failed: {exc}",
                ToolErrorCode.SERVICE_UNAVAILABLE,
            ) from exc

        # 传输成功不代表业务契约合法；必须在边界处再次进行 Pydantic 校验。
        payload = _extract_payload(result)
        return McpToolResponse.model_validate(payload)

    def _server_parameters(self) -> StdioServerParameters:
        """构造启动 MCP 子进程所需的解释器、环境、编码和工作目录参数。

        复制当前环境以保留测试配置，再强制 PYTHONUTF8，避免 Windows 本地编码破坏 JSON；严格
        编码错误处理会显式失败而非替换字符。使用 `sys.executable -m` 确保服务端与客户端共享
        同一个已安装依赖环境。
        """

        # 复制而非原地修改 os.environ，避免客户端配置影响当前进程和其他并发测试。
        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self._server_module],
            env=environment,
            cwd=str(self._cwd),
            encoding="utf-8",
            encoding_error_handler="strict",
        )

    def _session(self):
        """返回负责完整 stdio 与 ClientSession 生命周期的异步上下文管理器。

        封装两层 SDK context 可以保证调用方只处理初始化完成的 ClientSession，并让退出路径按
        反向顺序关闭协议会话和子进程管道；实际 I/O 延迟到 `async with` 进入时发生。
        """

        return _McpSessionContext(self._server_parameters())


class _McpSessionContext:
    """按正确嵌套顺序组合 SDK stdio transport 与 MCP ClientSession 生命周期。

    该内部适配器解决两个异步上下文必须同时保存并反向退出的问题；它不吞异常，退出参数原样
    传给 SDK，使取消、协议错误和正常关闭都执行相同资源清理路径。
    """

    def __init__(self, parameters: StdioServerParameters) -> None:
        """保存不可变启动参数，并初始化尚未进入的上下文引用。

        使用 None 表示某一层尚未成功创建，使 `__aexit__` 能处理 initialize 中途失败的部分初始化
        状态，不会因为清理不存在的会话而覆盖原始异常。
        """

        self._parameters = parameters
        self._stdio_context = None
        self._session_context = None

    async def __aenter__(self) -> ClientSession:
        """启动子进程管道、创建 ClientSession 并完成 MCP initialize 握手。

        只有 initialize 成功后才向调用方返回会话，保证 list/call 不会运行在未协商协议版本的
        transport 上。每一层上下文都保存在实例中，以便失败或正常退出时精确反向释放。
        """

        # 第一层拥有子进程和 stdio 管道，必须比使用这些流的 ClientSession 更晚退出。
        self._stdio_context = stdio_client(self._parameters, errlog=sys.stderr)
        read_stream, write_stream = await self._stdio_context.__aenter__()

        # 第二层负责 MCP 消息会话；initialize 是协议协商，不可用“能写入管道”替代。
        self._session_context = ClientSession(read_stream, write_stream)
        session = await self._session_context.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """按 ClientSession 后、stdio transport 前的顺序清理部分或完整会话。

        退出信息原样传递给 SDK，以便其区分取消和正常完成。None 检查支持 enter 在任意阶段失败；
        本方法不抑制异常，调用方仍能把原始错误映射成可审计的 `McpClientError`。
        """

        # 使用方会话先停止读写，再关闭承载它的管道和子进程，避免关闭顺序导致噪声异常。
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, traceback)
        if self._stdio_context is not None:
            await self._stdio_context.__aexit__(exc_type, exc, traceback)


def _extract_payload(result: CallToolResult) -> dict[str, Any]:
    """从 MCP CallToolResult 提取结构化字典，并拒绝协议错误或不可解析内容。

    优先使用规范的 `structuredContent`；兼容分支仅解析 TextContent 中的 JSON 对象，以支持不同
    SDK/服务端版本。`isError` 始终先处理，防止错误文本碰巧是 JSON 时被误当成功响应。没有合法
    字典则抛出 INTERNAL_ERROR，禁止客户端凭空构造默认成功载荷。
    """

    if result.isError:
        # 聚合所有文本块保留服务端诊断，同时忽略图片等本项目不支持的内容类型。
        message = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        raise McpClientError(
            message or "MCP tool returned an error",
            ToolErrorCode.INTERNAL_ERROR,
        )

    # 规范结构化字段具有最高优先级，避免对 SDK 已解析的数据做二次文本转换。
    if result.structuredContent is not None:
        return result.structuredContent

    # 文本 JSON 是兼容旧返回形式的受限回退，只接受顶层对象以匹配统一响应 Schema。
    for block in result.content:
        if not isinstance(block, TextContent):
            continue
        try:
            payload = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise McpClientError(
        "MCP tool returned no structured JSON payload",
        ToolErrorCode.INTERNAL_ERROR,
    )
