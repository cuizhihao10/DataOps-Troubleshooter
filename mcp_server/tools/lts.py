"""LTS 调度状态、日志和依赖拓扑三个只读 MCP 工具。

三个函数共享统一执行辅助函数，确保资源、时间范围、场景和 trace 都经过同一 Pydantic
请求模型。具体响应来自脱敏 Fixture，工具本身不包含诊断规则。
"""

from app.domain.tooling import McpToolRequest, McpToolResponse, TimeRange, ToolName
from mcp_server.repository import get_fixture_tool_repository


async def get_task_status(
    resource_id: str,
    time_range: TimeRange,
    scenario_id: str,
    trace_id: str,
) -> McpToolResponse:
    """返回指定合成场景中的 LTS 任务状态观察，不执行调度写操作。

    四个参数由 FastMCP 按统一 Schema 解析，本函数只选择固定工具枚举并委托共享执行辅助函数；
    Pydantic 校验失败或 Fixture 未命中会成为显式协议错误/失败响应，不生成默认状态。
    """
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
    """返回指定时间窗内的脱敏 LTS 合成日志证据，不读取真实日志系统。

    处理器不分析根因或修改日志，只把协议参数转换为统一请求并从 scenario_id 驱动的 Fixture
    取值；同一输入可重复得到同一合成响应，便于 ReAct 与评测重放。
    """
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
    """返回 LTS 任务的合成上下游拓扑观察，供跨组件故障链调查使用。

    函数只暴露已脱敏 Fixture 中的关系事实，不访问编排平台或自动变更依赖；资源、时间、场景和
    trace 会先进入共享请求模型，确保三个 LTS 工具拥有完全一致的边界行为。
    """
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
    """构造统一 LTS 工具请求并委托缓存的只读 Fixture 仓储执行。

    集中辅助函数避免三个处理器在字段映射上漂移；McpToolRequest 在仓储查找前验证资源 ID、
    时间范围、场景格式和 trace。失败由 Pydantic 或仓储显式返回，本函数不捕获后伪装成功。
    """

    # 所有协议参数先收敛为共享领域模型，服务端仓储不会接收未经校验的松散字典。
    request = McpToolRequest(
        resource_id=resource_id,
        time_range=time_range,
        scenario_id=scenario_id,
        trace_id=trace_id,
    )
    return get_fixture_tool_repository().execute(tool_name, request)
