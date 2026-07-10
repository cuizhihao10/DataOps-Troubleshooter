"""BDS 任务状态、执行日志和表信息三个只读 MCP 工具。

状态工具提供阶段与资源线索，日志工具提供错误和性能线索，表工具提供分区与统计信息。
三者只返回观察事实，不自行判断资源不足、数据倾斜或上游故障。
"""

from app.domain.tooling import McpToolRequest, McpToolResponse, TimeRange, ToolName
from mcp_server.repository import get_fixture_tool_repository


async def get_task_status(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """Return deterministic synthetic BDS task status and resource evidence."""
    return _execute(
        ToolName.BDS_GET_TASK_STATUS,
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
    """Return deterministic, sanitized BDS task log and performance evidence."""
    return _execute(
        ToolName.BDS_GET_TASK_LOG,
        resource_id,
        time_range,
        scenario_id,
        trace_id,
    )


async def get_table_info(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """Return deterministic synthetic BDS table metadata and partition evidence."""
    return _execute(
        ToolName.BDS_GET_TABLE_INFO,
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
