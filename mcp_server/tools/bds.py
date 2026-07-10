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
    """返回合成 BDS 任务阶段与资源指标观察，不操作真实计算集群。

    处理器只选择 `bds.get_task_status` 枚举并复用统一请求/仓储路径；状态中的资源压力只是证据，
    根因是否成立仍由 Planner 结合日志和其他组件 Observation 判断。
    """
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
    """返回脱敏 BDS 合成执行日志与性能证据，不在工具层推断根因。

    scenario_id 决定可重放响应，time_range 和 resource_id 仍经过统一 Schema 校验；Fixture 缺失
    会返回 EMPTY_RESULT，权限或服务异常按预置错误保留给客户端重试策略处理。
    """
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
    """返回合成 BDS 表结构、分区和统计信息，供验证数据侧假设使用。

    工具保持只读且封闭世界，不连接元数据生产服务，也不修改表；返回仅是 Observation，必须与
    本次实时工具证据和图路径共同审查，不能单独自动生成修复操作。
    """
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
    """将 BDS FastMCP 参数转换成统一请求并调用只读合成仓储。

    共享实现保证三个 BDS 工具在时区、ID 格式和 trace 处理上完全一致；任何参数错误由
    McpToolRequest 立即拒绝，仓储未命中则返回结构化错误且 evidence 为空。
    """

    # 在进入 Fixture 边界前集中校验，避免每个工具手写不一致的参数检查。
    request = McpToolRequest(
        resource_id=resource_id,
        time_range=time_range,
        scenario_id=scenario_id,
        trace_id=trace_id,
    )
    return get_fixture_tool_repository().execute(tool_name, request)
