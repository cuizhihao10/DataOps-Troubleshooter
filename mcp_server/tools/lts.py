from app.domain.tooling import McpToolRequest, McpToolResponse, TimeRange, ToolName
from mcp_server.repository import get_fixture_tool_repository


async def get_task_status(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """Return deterministic synthetic LTS task status evidence."""
    return _execute(
        ToolName.LTS_GET_TASK_STATUS,
        resource_id,
        time_range,
        scenario_id,
        trace_id,
    )


async def get_task_log(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """Return deterministic, sanitized LTS task log evidence."""
    return _execute(
        ToolName.LTS_GET_TASK_LOG,
        resource_id,
        time_range,
        scenario_id,
        trace_id,
    )


async def get_dependency_topology(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """Return deterministic LTS upstream and downstream dependency evidence."""
    return _execute(
        ToolName.LTS_GET_DEPENDENCY_TOPOLOGY,
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
