"""与传输无关的 MCP 客户端契约：错误分类、工具描述符、载荷提取与客户端 Protocol。

stdio 与 Streamable HTTP 是同一份工具契约的两种传输，因此错误码映射、工具注解快照和
`CallToolResult` 解析规则必须只写一份；否则两条路径会各自漂移，"契约与传输无关"就只剩口号。
本模块只定义边界类型与纯函数，不持有子进程、连接池或事件循环资源，因此可以被服务端测试、
两个客户端实现和执行器同时导入而不产生启动副作用。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict

from app.domain.tooling import McpToolRequest, McpToolResponse, ToolErrorCode, ToolName

# 传输选型单列一个契约 ID：九个工具名与 `McpToolResponse` 一个字未改，因此 `mcp-tools:v1` 保持不动。
# 这与 `model-transient-retry:v1` 是同一条理由——包装/传输层的变化不该假装成被包装契约的变化，
# 否则升一次传输就要把工具契约版本一起推高，读者再也分不清"工具变了"还是"连线方式变了"。
MCP_TRANSPORT_CONTRACT_ID = "mcp-transport:v1"


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


@runtime_checkable
class McpToolClient(Protocol):
    """声明应用侧真正依赖的三个 MCP 客户端能力，把传输选择挡在执行器之外。

    执行器只调用 `call_tool`，启动审计只调用两个发现方法；用 Protocol 而不是基类，是因为两个实现
    的资源模型完全不同（stdio 拥有子进程，HTTP 拥有连接池），共同基类会诱导把某一方的生命周期
    假设写进公共代码。`runtime_checkable` 让测试可以断言替身满足协议，但仍只检查方法是否存在。
    """

    async def list_tools(self) -> tuple[str, ...]:
        """返回经真实协议发现、按名称排序的工具名元组，供启动审计比对九项齐全。

        实现必须来自一次真实 `list_tools` 往返，不得回退到本地 `ToolName` 枚举冒充成功；发现失败
        应抛 `McpClientError` 并带分类错误码，让调用方能区分"服务不可达"与"工具确实缺失"。
        """

    async def list_tool_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        """返回带只读/非破坏/幂等/输出 Schema 注解的完整工具描述符元组。

        名称与安全注解必须来自同一次协议响应，否则"名字齐全但注解漂移"这种情况会被拆成两次查询
        而漏掉；缺失注解应保守转换为 False，让调用方有机会拒绝未声明只读的服务端。
        """

    async def call_tool(self, tool_name: ToolName, request: McpToolRequest) -> McpToolResponse:
        """执行一次白名单工具调用，并把协议载荷校验为统一响应模型。

        入参已在领域层完成白名单与字段校验，实现只负责传输、超时与载荷提取；任何未拿到合法响应
        的失败都必须抛 `McpClientError`，绝不返回空成功，否则执行器会把传输故障记成"工具无证据"。
        """


def to_tool_descriptors(tools: Iterable[Tool]) -> tuple[McpToolDescriptor, ...]:
    """把一次协议发现的 SDK Tool 列表投影成按名称排序的可审计描述符元组。

    只抽取安全门禁需要的字段，避免把 SDK 对象泄漏到应用领域层；缺失注解保守转换为 False，因此
    "服务端没声明只读"不会被读成"声明了只读"。排序让健康快照与集成测试断言稳定，两种传输共用
    这一份投影，注解审计因此不可能只在其中一条路径上生效。
    """

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
                for tool in tools
            ),
            key=lambda descriptor: descriptor.name,
        )
    )


def extract_payload(result: CallToolResult) -> dict[str, Any]:
    """从 MCP CallToolResult 提取结构化字典，并拒绝协议错误或不可解析内容。

    优先使用规范的 `structuredContent`；兼容分支仅解析 TextContent 中的 JSON 对象，以支持不同
    SDK/服务端版本。`isError` 始终先处理，防止错误文本碰巧是 JSON 时被误当成功响应。没有合法
    字典则抛出 INTERNAL_ERROR，禁止客户端凭空构造默认成功载荷。该函数完全与传输无关，因此
    stdio 与 Streamable HTTP 共用同一份解析规则，不存在"某条传输更宽松"的可能。
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
