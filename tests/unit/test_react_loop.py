"""验证 LangGraph 有界 ReAct 控制器的状态流转、策略门禁和停止语义。

单元测试使用结构化 Planner/Executor 替身，不模拟 LLM 文本或 MCP 协议；目标是精确证明
capability 注入、Observation 回写、同参去重、组件范围、步数和墙钟预算由确定性图控制。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.agents.planner import PlannerTurnContext
from app.capabilities import CapabilityName, CapabilitySelectionRequest, DiagnosisIntent
from app.domain.models import AgentState, Component
from app.domain.planner import PlannerDecision, PlannerStatus, ToolAction
from app.domain.tooling import McpToolResponse, ToolEvidencePayload, ToolName
from app.mcp.observation import ToolObservation, normalize_observation
from app.orchestration import (
    BoundedReactLoop,
    ReactEventType,
    ReactLoopConfig,
    ReactRunRequest,
    ReactStopReason,
)

OBSERVED_AT = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)


class ScriptedPlanner:
    """按预设顺序返回结构化决策，并保存每轮 PlannerTurnContext。

    该替身只用于测试控制器，不根据场景生成答案；决策耗尽时显式失败，防止图多调用 Planner
    却被默认 finish 掩盖。contexts 可验证 Observation 和 capability 是否进入下一轮。
    """

    def __init__(self, decisions: list[PlannerDecision]) -> None:
        """复制预设决策列表并初始化空的调用上下文记录。

        复制输入避免测试在运行后观察到原列表被就地消费；输出通过 PlannerDecision 构造时已完成
        Schema 校验。空列表允许超时等测试替换 decide 行为，但常规调用会抛出断言错误。
        """

        self._decisions = list(decisions)
        self.contexts: list[PlannerTurnContext] = []

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """记录当前强类型上下文并返回下一项预设决策。

        方法不执行 I/O；若没有剩余决策则抛出 AssertionError，表示 LangGraph 控制流超过测试预期。
        这种失败不能被控制器吞掉，从而能暴露预算或条件边配置错误。
        """

        self.contexts.append(context)
        if not self._decisions:
            raise AssertionError("planner was called more times than expected")
        return self._decisions.pop(0)


class BlockingPlanner:
    """永久等待取消的 Planner 替身，用于验证总墙钟预算。

    decide 会记录上下文后等待一个永不设置的事件；`asyncio.timeout` 应取消该等待并返回保留路由
    状态的 total_timeout 结果，而不是让测试或生产请求无限挂起。
    """

    def __init__(self) -> None:
        """初始化上下文记录与仅当前实例持有的未触发异步事件。

        每个测试创建独立实例，避免事件跨测试循环绑定；构造不启动后台任务，也不会产生未清理
        协程。实际等待只在 decide 被 LangGraph 调用后发生。
        """

        self.contexts: list[PlannerTurnContext] = []
        self._never_set = asyncio.Event()

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """记录上下文并等待控制器的总超时取消当前协程。

        该方法按类型声明返回 PlannerDecision，但正常路径不会返回；若事件被意外设置，显式抛出
        AssertionError，防止测试因无效决策产生与超时无关的结果。
        """

        self.contexts.append(context)
        await self._never_set.wait()
        raise AssertionError("blocking planner should only finish through cancellation")


class RecordingExecutor:
    """记录收到的 ToolAction，并返回确定性成功 ToolObservation。

    替身复用生产 `normalize_observation` 创建 Evidence 和 ToolEvent，因此单元测试仍验证回写模型
    和稳定 ID，不跨 MCP 子进程。策略门禁测试通过 actions 长度证明违规调用没有到达执行边界。
    """

    def __init__(self) -> None:
        """初始化空 Action 记录，不预先构造任何响应或证据。

        每次 execute 根据请求资源生成唯一 source_id，使不同参数调用可被区分；实例没有重试逻辑，
        因为本测试只验证 ReAct 步数与 MCP 尝试次数的边界分工。
        """

        self.actions: list[ToolAction] = []

    async def execute(self, action: ToolAction) -> ToolObservation:
        """记录 Action，并用统一生产适配器生成一条成功证据和一次事件。

        响应时间固定以保证测试可重放，source_id 包含 resource_id 以避免不同请求碰撞。输入若未
        通过控制器门禁本方法不应被调用；成功路径不抛异常且不模拟内部重试。
        """

        self.actions.append(action)
        response = McpToolResponse(
            ok=True,
            data={"status": "synthetic_ok"},
            evidence=[
                ToolEvidencePayload(
                    source_id=f"source_{action.arguments.resource_id}",
                    content=f"Synthetic observation for {action.arguments.resource_id}",
                )
            ],
            observed_at=OBSERVED_AT,
        )
        return normalize_observation(
            action=action,
            response=response,
            started_at=OBSERVED_AT,
            completed_at=OBSERVED_AT,
            attempt=1,
        )


def _state(*, run_id: str = "run_react_unit_001") -> AgentState:
    """构造带稳定运行、会话和脱敏问题的最小 AgentState。

    辅助函数不预填意图或 capability，确保测试能证明 select_capabilities 节点完成注入；可覆盖
    run_id 以测试 trace 绑定。返回模型通过与生产相同的 Pydantic 边界。
    """

    return AgentState(
        run_id=run_id,
        session_id="session_react_unit_001",
        user_query="检查合成 LTS 任务失败原因",
    )


def _run_request(*, component: Component = Component.LTS) -> ReactRunRequest:
    """构造单组件 ReAct 请求，供控制流和策略门禁测试复用。

    capability_request 使用显式意图与组件，不从自然语言猜路由；返回对象把空 AgentState 与固定
    registry 输入绑定，后续测试可独立替换 Planner 决策而不复制路由样板。
    """

    return ReactRunRequest(
        state=_state(),
        capability_request=CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(component,),
        ),
    )


def _action_decision(
    *,
    tool_name: ToolName = ToolName.LTS_GET_TASK_STATUS,
    trace_id: str = "run_react_unit_001",
    resource_id: str = "lts_synthetic_task",
) -> PlannerDecision:
    """构造一个字段完整、可执行的 call_tool PlannerDecision。

    时间窗、场景和 trace 都使用脱敏稳定值；允许覆盖工具与 trace 以验证组件越界和链路绑定。
    返回值先经过嵌套 ToolAction/McpToolRequest 校验，测试不会用松散字典绕过生产 Schema。
    """

    return PlannerDecision.model_validate(
        {
            "status": "call_tool",
            "decision_summary": "查询当前合成任务状态。",
            "hypothesis_updates": [],
            "action": {
                "tool_name": tool_name.value,
                "arguments": {
                    "resource_id": resource_id,
                    "time_range": {
                        "start": "2026-07-10T00:00:00+00:00",
                        "end": "2026-07-10T03:00:00+00:00",
                    },
                    "scenario_id": "cross_chain_pk_conflict",
                    "trace_id": trace_id,
                },
            },
            "evidence_refs": [],
            "stop_reason": None,
        }
    )


def _finish_decision(
    *,
    evidence_refs: list[str] | None = None,
    stop_reason: str = "evidence_sufficient",
) -> PlannerDecision:
    """构造不带 Action 的合法 finish 决策，并允许注入待校验引用。

    默认停止原因对应 Golden Case 语义；evidence_refs 可用于验证合法引用传递或模型虚构引用被
    控制器拦截。该辅助函数不生成 Thought，也不声称任何根因已经成立。
    """

    return PlannerDecision(
        status=PlannerStatus.FINISH,
        decision_summary="结束当前合成调查。",
        evidence_refs=evidence_refs or [],
        stop_reason=stop_reason,
    )


@pytest.mark.asyncio
async def test_langgraph_loop_injects_capabilities_records_observation_and_finishes() -> None:
    """验证一个 Action 真正经过 LangGraph 执行并在第二轮 Planner finish。

    断言 capability 注入、两轮 Planner、单个 ReAct 步骤、Evidence/ToolEvent 回写和公开事件顺序；
    Executor 仅一次调用证明内部图没有重复执行，最终停止原因来自结构化 Planner 决策。
    """

    planner = ScriptedPlanner([_action_decision(), _finish_decision()])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.intent == DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS.value
    assert result.state.active_capabilities == [
        CapabilityName.SINGLE_COMPONENT_DIAGNOSIS.value,
        CapabilityName.RISK_ASSESSMENT.value,
        CapabilityName.STRUCTURED_REPORTING.value,
    ]
    assert result.state.react_step == 1
    assert result.state.stop_reason == "evidence_sufficient"
    assert len(result.state.evidence) == 1
    assert len(result.state.tool_events) == 1
    assert len(planner.contexts) == 2
    assert planner.contexts[1].state.observation_refs == result.state.observation_refs
    assert len(executor.actions) == 1
    assert [event.event_type for event in result.events] == [
        ReactEventType.CAPABILITIES_SELECTED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.LOOP_STOPPED,
    ]


@pytest.mark.asyncio
async def test_need_user_input_stops_without_calling_executor() -> None:
    """验证 Planner 请求关键补参时保留公开原因并直接结束图。

    need_user_input 与 finish 共用非 Action 分支，但本测试单独确认不会误入 execute_tool；状态中的
    next_action 保留结构化决策，stop_reason 可供未来 API 向用户解释缺少的输入。
    """

    planner = ScriptedPlanner(
        [
            PlannerDecision(
                status=PlannerStatus.NEED_USER_INPUT,
                decision_summary="缺少无法通过只读工具获得的任务标识。",
                stop_reason="missing_resource_id",
            )
        ]
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == "missing_resource_id"
    assert result.state.next_action is not None
    assert result.state.next_action.status is PlannerStatus.NEED_USER_INPUT
    assert result.state.react_step == 0
    assert executor.actions == []


@pytest.mark.asyncio
async def test_same_tool_with_different_parameters_executes_as_two_actions() -> None:
    """验证相同工具查询不同资源不会被同参指纹误拦截或产生事件冲突。

    两个 LTS status Action 只改变 resource_id，应分别执行并写入两条 Evidence/ToolEvent；第三轮
    finish 后 react_step 为二。该用例连接重复策略与请求身份审计 ID 两个实现边界。
    """

    planner = ScriptedPlanner(
        [
            _action_decision(resource_id="lts_synthetic_task_a"),
            _action_decision(resource_id="lts_synthetic_task_b"),
            _finish_decision(),
        ]
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == "evidence_sufficient"
    assert result.state.react_step == 2
    assert len(result.state.evidence) == 2
    assert len(result.state.tool_events) == 2
    assert len({event.event_id for event in result.state.tool_events}) == 2
    assert len(executor.actions) == 2


@pytest.mark.asyncio
async def test_duplicate_action_is_blocked_before_second_executor_call() -> None:
    """验证 Planner 重复同一工具与规范化参数时循环安全停止。

    第一次 Action 产生 Observation，第二次相同决策只生成公开 policy_blocked 事件；executor
    actions 仍为一项且 react_step 不增加，证明重复检测位于 MCP 外部并未消耗第三次调用。
    """

    action = _action_decision()
    planner = ScriptedPlanner([action, action])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.DUPLICATE_ACTION_BLOCKED.value
    assert result.state.react_step == 1
    assert len(executor.actions) == 1
    assert result.events[-1].event_type is ReactEventType.POLICY_BLOCKED


@pytest.mark.asyncio
async def test_component_scope_blocks_out_of_capability_tool() -> None:
    """验证 LTS 单组件路由不能执行合法白名单中的 BDS 工具。

    工具名本身通过 ToolName Schema，但不属于 capability selection 的组件范围，因此控制器必须
    在 MCP 前停止。零 executor Action 证明全局白名单不能替代本轮最小权限边界。
    """

    planner = ScriptedPlanner([_action_decision(tool_name=ToolName.BDS_GET_TASK_STATUS)])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.TOOL_NOT_ALLOWED_BY_CAPABILITY.value
    assert result.state.react_step == 0
    assert executor.actions == []


@pytest.mark.asyncio
async def test_react_budget_stops_before_an_extra_planner_or_tool_call() -> None:
    """验证达到 max_steps 后图在下一 Planner 调用前停止。

    最大步骤设为一，首个 Action 正常写回；循环返回 planner 节点时先检查预算，不消费第二个
    预设决策，也不执行额外工具。react_step 与 executor 数量都恰好为一。
    """

    planner = ScriptedPlanner([_action_decision()])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=1, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.REACT_BUDGET_EXHAUSTED.value
    assert result.state.react_step == 1
    assert len(planner.contexts) == 1
    assert len(executor.actions) == 1


@pytest.mark.asyncio
async def test_total_timeout_cancels_blocked_planner_and_preserves_route_event() -> None:
    """验证墙钟预算可以取消卡住的 Planner 并保留已完成路由状态。

    BlockingPlanner 不会自行返回；控制器应在短预算内生成 total_timeout，而不是传播 CancelledError
    或丢失 capability selection。Executor 未调用，事件仍包含 selected 和 stopped 两项。
    """

    planner = BlockingPlanner()
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=0.05),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.TOTAL_TIMEOUT.value
    assert result.state.active_capabilities
    assert len(planner.contexts) == 1
    assert executor.actions == []
    assert [event.event_type for event in result.events] == [
        ReactEventType.CAPABILITIES_SELECTED,
        ReactEventType.LOOP_STOPPED,
    ]


@pytest.mark.asyncio
async def test_invalid_evidence_reference_and_trace_are_blocked() -> None:
    """验证模型虚构 evidence_id 或使用其他 run 的 trace 都不能进入外部执行。

    两次独立运行分别覆盖引用一致性与 trace 绑定；前者在 finish 前停止，后者在 MCP 前停止。
    两个 executor 均为空，证明格式合法的 Planner JSON 仍需确定性语义门禁。
    """

    invalid_ref_executor = RecordingExecutor()
    invalid_ref_loop = BoundedReactLoop(
        planner=ScriptedPlanner([_finish_decision(evidence_refs=["ev_missing"])]),
        executor=invalid_ref_executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )
    invalid_ref_result = await invalid_ref_loop.run(_run_request())

    trace_executor = RecordingExecutor()
    trace_loop = BoundedReactLoop(
        planner=ScriptedPlanner([_action_decision(trace_id="run_other_001")]),
        executor=trace_executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )
    trace_result = await trace_loop.run(_run_request())

    assert invalid_ref_result.state.stop_reason == ReactStopReason.INVALID_EVIDENCE_REFERENCE.value
    assert trace_result.state.stop_reason == ReactStopReason.TRACE_ID_MISMATCH.value
    assert invalid_ref_executor.actions == []
    assert trace_executor.actions == []
