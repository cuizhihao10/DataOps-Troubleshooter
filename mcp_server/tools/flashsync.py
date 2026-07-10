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
    """Return deterministic synthetic synchronization delay and backlog evidence."""
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
    """Return deterministic, sanitized synchronization error evidence."""
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
    """Return deterministic source and target consistency sample evidence."""
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
    request = McpToolRequest(
        resource_id=resource_id,
        time_range=time_range,
        scenario_id=scenario_id,
        trace_id=trace_id,
    )
    return get_fixture_tool_repository().execute(tool_name, request)
