"""通过 LangGraph 与真实 stdio MCP 验证 Planner Action/Observation 多轮闭环。

测试 Planner 是可控结构化替身，以隔离尚未实现的模型供应商；工具执行必须使用官方 MCP SDK
启动独立 FastMCP 进程。该组合证明图节点没有直接读取 Fixture，也没有伪造 Observation。
"""

import pytest

from app.agents.planner import PlannerTurnContext
from app.capabilities import CapabilitySelectionRequest, DiagnosisIntent
from app.domain.models import AgentState, Component
from app.domain.planner import PlannerDecision
from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor
from app.orchestration import (
    BoundedReactLoop,
    ReactEventType,
    ReactLoopConfig,
    ReactRunRequest,
)


class OneToolProtocolPlanner:
    """首轮请求 LTS 状态，收到真实 Observation 后第二轮结束调查。

    该替身不解释工具内容或生成根因，只根据 react_step 选择预先批准的结构化分支。保存 contexts
    可证明第二轮 Planner 确实看到 MCP 写回的 Evidence 与 ToolEvent，而不是测试直接注入结果。
    """

    def __init__(self) -> None:
        """初始化空上下文记录，供测试检查两轮状态传播。

        构造不启动 MCP、模型或后台任务；所有外部 I/O 只会发生在 LangGraph 的 execute_tool
        节点中，因此 Planner 替身无法绕过协议边界读取 Fixture。
        """

        self.contexts: list[PlannerTurnContext] = []

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """按 react_step 返回一次 call_tool，随后引用 Observation 并 finish。

        首轮参数使用当前 run_id 作为 trace，满足控制器链路绑定；第二轮仅引用状态中真实存在的
        observation_refs。若图错误调用第三轮，显式断言失败而不是返回默认结果。
        """

        self.contexts.append(context)
        if context.state.react_step == 0:
            return PlannerDecision.model_validate(
                {
                    "status": "call_tool",
                    "decision_summary": "先查询 LTS 合成任务状态。",
                    "hypothesis_updates": [],
                    "action": {
                        "tool_name": "lts.get_task_status",
                        "arguments": {
                            "resource_id": "dws_order_report_daily",
                            "time_range": {
                                "start": "2026-07-10T00:00:00+08:00",
                                "end": "2026-07-10T03:00:00+08:00",
                            },
                            "scenario_id": "cross_chain_pk_conflict",
                            "trace_id": context.state.run_id,
                        },
                    },
                    "evidence_refs": [],
                    "stop_reason": None,
                }
            )
        if context.state.react_step == 1:
            return PlannerDecision.model_validate(
                {
                    "status": "finish",
                    "decision_summary": "已获得本轮真实状态 Observation。",
                    "hypothesis_updates": [],
                    "action": None,
                    "evidence_refs": context.state.observation_refs,
                    "stop_reason": "evidence_sufficient",
                }
            )
        raise AssertionError("protocol Planner should be called exactly twice")


@pytest.mark.asyncio
async def test_langgraph_react_loop_crosses_real_mcp_and_returns_to_planner() -> None:
    """验证 LangGraph 完成 Planner→真实 MCP→Observation→Planner 的完整一轮。

    断言两轮上下文、单个 ReAct Action、真实工具响应、证据/事件回写及公开时间线；第二轮引用
    必须等于第一轮 MCP 生成的 observation_refs，证明 Planner 没有自行编造 Observation。
    """

    planner = OneToolProtocolPlanner()
    loop = BoundedReactLoop(
        planner=planner,
        executor=McpToolExecutor(StdioMcpClient(), retry_count=1),
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=15),
    )
    request = ReactRunRequest(
        state=AgentState(
            run_id="run_react_protocol_001",
            session_id="session_react_protocol_001",
            user_query="检查 LTS 合成任务失败原因",
        ),
        capability_request=CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        ),
    )

    result = await loop.run(request)

    assert len(planner.contexts) == 2
    assert planner.contexts[0].state.evidence == []
    assert planner.contexts[1].state.evidence
    assert planner.contexts[1].state.tool_events
    assert result.state.react_step == 1
    assert result.state.stop_reason == "evidence_sufficient"
    assert result.state.tool_events[0].tool_name.value == "lts.get_task_status"
    assert result.state.tool_events[0].response.data["status"] == "failed"
    assert result.state.observation_refs == [item.evidence_id for item in result.state.evidence]
    assert [event.event_type for event in result.events] == [
        ReactEventType.CAPABILITIES_SELECTED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.LOOP_STOPPED,
    ]
