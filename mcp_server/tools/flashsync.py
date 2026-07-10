"""FlashSync 延迟、同步日志和一致性抽检三个只读 MCP 工具。

这些工具覆盖同步链路的时效、错误和结果一致性三个观察维度。返回值由 scenario_id 决定，
不会触发真实同步、修复冲突或修改位点。
"""

from app.domain.tooling import McpToolRequest, McpToolResponse, TimeRange, ToolName
from mcp_server.repository import get_fixture_tool_repository


async def get_sync_delay(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """返回合成 FlashSync 延迟、吞吐与积压观察，不触发同步或扩容。

    处理器通过统一请求模型和 Fixture 仓储提供确定性只读证据；延迟值本身不等于根因，Planner
    仍需结合上游状态、同步日志和一致性抽检形成有引用结论。
    """
    return _execute(
        ToolName.FLASHSYNC_GET_SYNC_DELAY,
        resource_id,
        time_range,
        scenario_id,
        trace_id,
    )


async def get_sync_log(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """返回脱敏 FlashSync 合成错误与冲突日志，不接触生产日志源。

    time_range 限定观察窗口，trace_id 连接同一次诊断；工具层只返回事实或标准错误，不自动修复
    冲突、修改位点，也不把历史 Fixture 描述成实时生产状态。
    """
    return _execute(
        ToolName.FLASHSYNC_GET_SYNC_LOG,
        resource_id,
        time_range,
        scenario_id,
        trace_id,
    )


async def check_consistency(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """返回合成源端与目标端一致性抽检结果，不执行全量校验或数据修复。

    抽检响应由 scenario_id 固定，适合演示证据链和失败降级；其范围限制必须保留在证据元数据中，
    Auditor 可据此防止把样本结果夸大为全量一致性结论。
    """
    return _execute(
        ToolName.FLASHSYNC_CHECK_CONSISTENCY,
        resource_id,
        time_range,
        scenario_id,
        trace_id,
    )


def _execute(
    tool_name: ToolName,
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """构造统一 FlashSync 请求并从缓存 Fixture 仓储读取只读响应。

    集中字段映射确保延迟、日志和一致性三个处理器共享相同时区、场景和 trace 校验；仓储只做
    精确匹配，任何缺失都返回标准 EMPTY_RESULT，而不是选择近似资源或默认成功。
    """

    # 先用领域模型收紧协议参数，再进入唯一允许读取合成 Fixture 的仓储边界。
    request = McpToolRequest(
        resource_id=resource_id,
        time_range=time_range,
        scenario_id=scenario_id,
        trace_id=trace_id,
    )
    return get_fixture_tool_repository().execute(tool_name, request)
