"""验证 LangGraph 有界 ReAct 控制器的状态流转、策略门禁和停止语义。

单元测试使用结构化 Planner/Executor 替身，不模拟 LLM 文本或 MCP 协议；目标是精确证明
capability 注入、Observation 回写、同参去重、组件范围、步数和墙钟预算由确定性图控制。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.agents.planner import PlannerProviderError, PlannerTurnContext
from app.capabilities import CapabilityName, CapabilitySelectionRequest, DiagnosisIntent
from app.domain.models import AgentState, Component, HypothesisStatus
from app.domain.planner import (
    HypothesisUpdate,
    HypothesisUpdateStatus,
    PlannerDecision,
    PlannerStatus,
    PlannerStopReason,
    ToolAction,
)
from app.domain.tooling import McpToolResponse, ToolEvidencePayload, ToolName
from app.mcp.observation import ToolObservation, normalize_observation
from app.orchestration import (
    BoundedReactLoop,
    ReactEventType,
    ReactLoopConfig,
    ReactRunRequest,
    ReactStopReason,
)
from app.retrieval.models import (
    BundledKnowledgeNode,
    EvidenceBundleBudget,
    GraphEvidenceBundle,
    KnowledgeNodeType,
    RetrievalMode,
)

OBSERVED_AT = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)


class ScriptedPlanner:
    """按预设顺序返回结构化决策，并保存每轮 PlannerTurnContext。

    该替身只用于测试控制器，不根据场景生成答案；决策耗尽时显式失败，防止图多调用 Planner
    却被默认 finish 掩盖。contexts 可验证 Observation 和 capability 是否进入下一轮。
    """

    def __init__(self, decisions: list[PlannerDecision | Exception]) -> None:
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
        outcome = self._decisions.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


class ConcurrencyProbeExecutor(RecordingExecutor):
    """在 RecordingExecutor 之上测量同一时刻真正在执行的 Action 数量。

    每次调用先自增在途计数并记录峰值，再让出事件循环，最后自减。峰值大于一是"批次确实并发"的
    唯一可靠证据：只看总耗时会被机器负载干扰，而只看 actions 列表无法区分并发与串行。
    """

    def __init__(self) -> None:
        """初始化在途计数与峰值记录，不改变父类的 Action 收集行为。

        计数只在单个事件循环内被协程交替修改，因此无需锁；峰值在测试断言前保持只增不减，
        便于在批次结束后回读整轮的最大并发度。
        """

        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0

    async def execute(self, action: ToolAction) -> ToolObservation:
        """在维护并发峰值的同时委托父类生成确定性 Observation。

        `asyncio.sleep(0.05)` 提供一个足以让兄弟协程全部进入执行体的让出点；串行实现会让峰值
        停在一，并行实现会把峰值推到批次长度。异常路径不会掩盖计数，因为自减放在 finally 中。
        """

        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            return await super().execute(action)
        finally:
            self.in_flight -= 1


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
    """构造一个字段完整、只含单个 Action 的 call_tool PlannerDecision。

    时间窗、场景和 trace 都使用脱敏稳定值；允许覆盖工具与 trace 以验证组件越界和链路绑定。
    返回值先经过嵌套 ToolAction/McpToolRequest 校验，测试不会用松散字典绕过生产 Schema。
    """

    return _batch_decision(
        specs=[(tool_name, resource_id)],
        trace_id=trace_id,
    )


def _batch_decision(
    *,
    specs: list[tuple[ToolName, str]],
    trace_id: str = "run_react_unit_001",
) -> PlannerDecision:
    """构造一个含任意条数并行 Action 的 call_tool 决策，供批次门禁测试复用。

    每个 spec 是"工具名 + 资源 ID"二元组，因此同一工具查不同资源和不同工具查同一资源都能表达；
    时间窗、场景与 trace 对整批保持一致，使指纹差异只来自测试真正想验证的那个维度。
    """

    return PlannerDecision.model_validate(
        {
            "status": "call_tool",
            "decision_summary": "查询当前合成任务状态。",
            "hypothesis_updates": [],
            "actions": [
                {
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
                }
                for tool_name, resource_id in specs
            ],
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
async def test_react_budget_grants_one_closing_turn_that_lets_planner_finish() -> None:
    """验证取证步数用尽后控制器额外发放一次收口回合，让 Planner 自己收口。

    最大步骤设为一，首个 Action 正常写回；循环返回 planner 节点时预算已耗尽，但控制器不直接终止，
    而是以批次上限 0 再调用一次 Planner。停止原因来自 Planner 的结构化 finish 而不是
    react_budget_exhausted，收口回合不消耗 react_step，也不产生第二次工具调用。
    """

    planner = ScriptedPlanner(
        [_action_decision(), _finish_decision(stop_reason="evidence_insufficient")]
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=1, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == "evidence_insufficient"
    assert result.state.react_step == 1
    assert result.state.closing_turn_used is True
    assert len(planner.contexts) == 2
    assert len(executor.actions) == 1
    closing_context = planner.contexts[-1]
    assert closing_context.closing_turn is True
    assert closing_context.max_parallel_actions == 0
    assert planner.contexts[0].closing_turn is False


@pytest.mark.asyncio
async def test_closing_turn_projects_hypotheses_even_when_planner_keeps_calling_tools() -> None:
    """验证收口回合越界提交 call_tool 时仍保留该轮假设更新，并以预算耗尽终止。

    收口回合的批次上限是 0，因此任何 Action 都越界，事件必须是 POLICY_BLOCKED 且停止原因回到
    react_budget_exhausted。但拦截发生在假设投影之后，所以模型这一轮写下的根因照常进入状态——
    报告层只认 AgentState.hypotheses，把它一起丢掉等于让一次越界抹掉全部可用结论。
    """

    closing_batch = _with_updates(
        _action_decision(),
        [
            HypothesisUpdate(
                hypothesis_id="hyp_closing_turn_projection",
                status=HypothesisUpdateStatus.NEW,
                symptom="LTS 任务在合成场景中持续失败。",
                candidate_root_cause="分区日期格式不合法导致调度拒绝提交。",
                evidence_refs=[],
            )
        ],
    )
    planner = ScriptedPlanner([_action_decision(), closing_batch])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=1, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.REACT_BUDGET_EXHAUSTED.value
    assert result.state.react_step == 1
    assert len(executor.actions) == 1
    assert [item.hypothesis_id for item in result.state.hypotheses] == [
        "hyp_closing_turn_projection"
    ]
    assert result.events[-1].event_type is ReactEventType.POLICY_BLOCKED


@pytest.mark.asyncio
async def test_closing_turn_is_not_granted_twice_after_a_resumed_state() -> None:
    """验证已消耗收口回合的状态不会再领到第二次，即使从外部恢复而来。

    cancel/resume 会把 AgentState 从 checkpoint 读回来，若额度只记在图内部状态里，恢复后的运行就能
    反复领取收口回合，"只发一次"变成事实上无界。这里直接注入 closing_turn_used=True 且预算已满的
    状态，断言控制器一次模型都不调用就以 react_budget_exhausted 停止。
    """

    planner = ScriptedPlanner([])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=1, total_timeout_seconds=2),
    )
    request = _run_request()
    exhausted = request.state.model_copy(update={"react_step": 1, "closing_turn_used": True})

    result = await loop.run(request.model_copy(update={"state": exhausted}))

    assert result.state.stop_reason == ReactStopReason.REACT_BUDGET_EXHAUSTED.value
    assert planner.contexts == []
    assert executor.actions == []


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


@pytest.mark.asyncio
async def test_restored_tool_event_blocks_same_action_with_new_run_trace() -> None:
    """验证 checkpoint 恢复后更换 run trace 仍不能重复同一语义工具查询。

    上一轮 ToolEvent 使用旧 run_id，本轮 Planner 必须使用新 run_id 才能通过 trace 门禁；重复指纹
    忽略这项审计身份差异，仍按工具、资源、时间窗和场景判定相同 Action，Executor 不应收到调用。
    """

    previous_decision = _action_decision(trace_id="run_previous_001")
    assert len(previous_decision.actions) == 1
    previous_observation = await RecordingExecutor().execute(previous_decision.actions[0])
    restored_state = _state().model_copy(
        update={"tool_events": list(previous_observation.tool_events)}
    )
    request = ReactRunRequest(
        state=restored_state,
        capability_request=CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        ),
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=ScriptedPlanner([_action_decision(trace_id=restored_state.run_id)]),
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(request)

    assert result.state.stop_reason == ReactStopReason.DUPLICATE_ACTION_BLOCKED.value
    assert executor.actions == []


@pytest.mark.asyncio
async def test_expected_planner_provider_error_becomes_public_loop_stop() -> None:
    """验证已净化 Provider 错误由 LangGraph 转换为终态而不是崩溃或执行工具。

    ScriptedPlanner 抛出稳定 PlannerProviderError；结果必须使用 planner_provider_error 和安全摘要，
    executor 保持空。未预期异常仍不在本测试覆盖范围，控制器应继续传播它们。
    """

    planner = ScriptedPlanner(
        [
            PlannerProviderError(
                error_code="timeout",
                public_summary="Planner 模型请求超过配置超时。",
                retryable=True,
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

    assert result.state.stop_reason == "planner_provider_error"
    assert result.events[-1].summary == "Planner 模型请求超过配置超时。"
    assert executor.actions == []


@pytest.mark.asyncio
async def test_parallel_batch_runs_concurrently_and_consumes_one_step_per_action() -> None:
    """验证一批三个互不依赖的只读 Action 并发执行并按条数消耗步数预算。

    并发度由执行器峰值计数证明，而不是靠总耗时推断；三条 Evidence/ToolEvent 说明批内 ID 不冲突，
    react_step 增加三而不是一说明并行买到的是延迟而不是额外取证预算。事件时间线保留一条
    PLANNER_DECISION 加三条 OBSERVATION_RECORDED，因此单个工具失败仍能被单独读出来。
    """

    planner = ScriptedPlanner(
        [
            _batch_decision(
                specs=[
                    (ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task"),
                    (ToolName.LTS_GET_TASK_LOG, "lts_synthetic_task"),
                    (ToolName.LTS_GET_DEPENDENCY_TOPOLOGY, "lts_synthetic_task"),
                ]
            ),
            _finish_decision(),
        ]
    )
    executor = ConcurrencyProbeExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, max_parallel_actions=3, total_timeout_seconds=5),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == "evidence_sufficient"
    assert result.state.react_step == 3
    assert executor.max_in_flight == 3
    assert len(executor.actions) == 3
    assert len(result.state.evidence) == 3
    assert len({event.event_id for event in result.state.tool_events}) == 3
    assert [event.event_type for event in result.events] == [
        ReactEventType.CAPABILITIES_SELECTED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.LOOP_STOPPED,
    ]
    decision_event = result.events[1]
    assert decision_event.parallel_action_count == 3
    # 批次刻意不填 tool_name：只写第一个工具会让时间线读起来像"只调用了一个工具"。
    assert decision_event.tool_name is None
    observation_tools = {event.tool_name for event in result.events[2:5]}
    assert observation_tools == {
        ToolName.LTS_GET_TASK_STATUS,
        ToolName.LTS_GET_TASK_LOG,
        ToolName.LTS_GET_DEPENDENCY_TOPOLOGY,
    }


@pytest.mark.asyncio
async def test_single_action_batch_still_reports_its_tool_name() -> None:
    """验证单 Action 批次仍在决策事件上保留具体工具名与批次大小一。

    并行改造不应让最常见的单调用路径失去可读性；前端和评测依赖 PLANNER_DECISION 上的 tool_name
    展示"这一步查了什么"，因此这项断言防止批次字段回归时把单调用也一起清空。
    """

    planner = ScriptedPlanner([_action_decision(), _finish_decision()])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, max_parallel_actions=3, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    decision_event = result.events[1]
    assert decision_event.parallel_action_count == 1
    assert decision_event.tool_name is ToolName.LTS_GET_TASK_STATUS
    assert result.state.react_step == 1


@pytest.mark.asyncio
async def test_duplicate_action_inside_one_batch_is_blocked_before_any_execution() -> None:
    """验证批内同参重复整批拒绝，并行不能成为绕过去重门禁的路径。

    两个 Action 的工具、资源、时间窗和场景完全一致，指纹相同；控制器必须在 MCP 之前停止整批，
    executor 保持为空，react_step 不增加。若只丢弃重复项，Planner 会基于"提交了两个"的错误前提
    继续推理，因此这里刻意不做部分执行。
    """

    planner = ScriptedPlanner(
        [
            _batch_decision(
                specs=[
                    (ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task"),
                    (ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task"),
                ]
            )
        ]
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, max_parallel_actions=3, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.DUPLICATE_ACTION_BLOCKED.value
    assert result.state.react_step == 0
    assert executor.actions == []
    assert result.events[-1].event_type is ReactEventType.POLICY_BLOCKED


@pytest.mark.asyncio
async def test_batch_over_configured_parallel_limit_is_blocked() -> None:
    """验证超过本次运行并行上限的批次被 parallel_limit_exceeded 拦下。

    运行配置只允许一个并行 Action，但 Planner 提交两个；上限属于部署决定而不是模型偏好，因此
    必须由控制器客观拒绝而不是靠 Prompt 提醒。executor 为空证明拦截发生在 MCP 边界之前。
    """

    planner = ScriptedPlanner(
        [
            _batch_decision(
                specs=[
                    (ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task"),
                    (ToolName.LTS_GET_TASK_LOG, "lts_synthetic_task"),
                ]
            )
        ]
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, max_parallel_actions=1, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.PARALLEL_LIMIT_EXCEEDED.value
    assert result.state.react_step == 0
    assert executor.actions == []


@pytest.mark.asyncio
async def test_batch_exceeding_remaining_step_budget_is_blocked() -> None:
    """验证批次长度超过剩余工具步数时整批拒绝，而不是执行到预算用尽为止。

    max_steps 为二、并行上限为二：首轮批次两个 Action 恰好用尽预算是允许的，因此本用例先执行
    一个单 Action 再提交两个，使剩余步数只剩一。控制器必须给出 parallel_budget_exceeded，
    executor 仍只收到第一轮那一次调用。
    """

    planner = ScriptedPlanner(
        [
            _action_decision(resource_id="lts_synthetic_task_a"),
            _batch_decision(
                specs=[
                    (ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task_b"),
                    (ToolName.LTS_GET_TASK_LOG, "lts_synthetic_task_b"),
                ]
            ),
        ]
    )
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=2, max_parallel_actions=2, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.PARALLEL_BUDGET_EXCEEDED.value
    assert result.state.react_step == 1
    assert len(executor.actions) == 1


@pytest.mark.asyncio
async def test_planner_context_parallel_allowance_shrinks_with_remaining_budget() -> None:
    """验证注入 Prompt 的并行上限始终等于控制器真正允许的数量。

    max_steps 为二、并行上限为三：首轮剩余两步，因此模型只能看到二；执行一个 Action 后剩余一步，
    第二轮必须降到一。这项断言把"Prompt 里的预算"和"门禁里的预算"绑成同一个事实，避免模型反复
    提交刚好超预算的批次而每次都白花一次调用。
    """

    planner = ScriptedPlanner([_action_decision(), _finish_decision()])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=2, max_parallel_actions=3, total_timeout_seconds=2),
    )

    await loop.run(_run_request())

    assert [context.max_parallel_actions for context in planner.contexts] == [2, 1]


_HYPOTHESIS_ID = "hyp_lts_invalid_partition_date_format"


def _with_updates(
    decision: PlannerDecision,
    updates: list[HypothesisUpdate],
) -> PlannerDecision:
    """在已构造的决策上替换 hypothesis_updates，并重新走一遍生产 Schema 校验。

    这里刻意不用 `model_copy`：跨字段校验器是契约的一部分，测试若绕过它就会用生产不可能出现的
    组合去断言控制器行为。重新 `model_validate` 保证假设更新与 status/actions/stop_reason 的组合
    仍然合法，因此断言的是真实可达路径。
    """

    return PlannerDecision.model_validate(
        {
            **decision.model_dump(mode="json"),
            "hypothesis_updates": [update.model_dump(mode="json") for update in updates],
        }
    )


class HypothesisPlanner:
    """按轮次提交假设更新，并把引用绑定到上一轮真正产生的 Evidence ID。

    真实 Planner 只能引用 Prompt 白名单里的 ID，而白名单要等工具执行后才非空，因此固定决策列表
    无法表达"新建 → 增强 → 削弱"这条多轮演进。该替身在每轮读取 `context.state.evidence` 的末尾
    一条来构造引用，从而在不接触 MCP 的前提下复现生产里唯一可达的引用顺序。
    """

    def __init__(self) -> None:
        """初始化空的上下文记录，不预置任何决策或引用。

        轮次由 `contexts` 长度推导，因此不需要额外计数器；实例不执行 I/O，也不生成 Thought，
        只组装已经通过 Pydantic 校验的结构化决策。
        """

        self.contexts: list[PlannerTurnContext] = []

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """依次返回带 new、strengthened 与 weakened 假设更新的三轮决策。

        第一轮尚无任何 Evidence，因此新建假设不带引用，用于验证"无引用的新假设只能停在
        candidate"；第二、三轮分别引用刚刚写回的 Observation，用于验证支持引用累积升级和反对
        引用回落。超过三轮说明控制流与预期不符，显式失败而不是静默补一个 finish。
        """

        self.contexts.append(context)
        turn = len(self.contexts)
        if turn == 1:
            return _with_updates(
                _batch_decision(specs=[(ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task")]),
                [
                    HypothesisUpdate(
                        hypothesis_id=_HYPOTHESIS_ID,
                        status=HypothesisUpdateStatus.NEW,
                        symptom="合成 LTS 任务连续三次调度失败。",
                        candidate_root_cause="partition_date 参数格式不符合调度约定的 yyyy-MM-dd。",
                    )
                ],
            )
        latest_ref = context.state.evidence[-1].evidence_id
        if turn == 2:
            return _with_updates(
                _batch_decision(specs=[(ToolName.LTS_GET_TASK_LOG, "lts_synthetic_task")]),
                [
                    HypothesisUpdate(
                        hypothesis_id=_HYPOTHESIS_ID,
                        status=HypothesisUpdateStatus.STRENGTHENED,
                        evidence_refs=[latest_ref],
                    )
                ],
            )
        if turn == 3:
            return _with_updates(
                _finish_decision(),
                [
                    HypothesisUpdate(
                        hypothesis_id=_HYPOTHESIS_ID,
                        status=HypothesisUpdateStatus.WEAKENED,
                        evidence_refs=[latest_ref],
                    )
                ],
            )
        raise AssertionError("planner was called more times than expected")


@pytest.mark.asyncio
async def test_hypothesis_updates_accumulate_into_state_with_deterministic_status() -> None:
    """验证假设更新被确定性投影进 AgentState，并按引用决定状态与置信度。

    这条链路此前完全缺失：控制器只保存 next_action，`hypothesis_updates` 被整体丢弃，于是确定性
    草稿永远拿不到根因、Auditor 必然以"报告不完整"否决。断言覆盖三件事——新建假设在没有引用时
    只能停在 candidate、带合法引用的增强升为 supported、削弱把引用记入反对集合并回落 candidate。
    组件取已批准 capability 组件而不是模型自述，置信度取状态映射而不是模型自报数值。
    """

    planner = HypothesisPlanner()
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert len(executor.actions) == 2
    assert [hypothesis.hypothesis_id for hypothesis in result.state.hypotheses] == [_HYPOTHESIS_ID]
    hypothesis = result.state.hypotheses[0]
    assert hypothesis.symptom == "合成 LTS 任务连续三次调度失败。"
    assert hypothesis.candidate_root_cause.startswith("partition_date")
    assert hypothesis.components == [Component.LTS]
    assert hypothesis.supporting_evidence == [result.state.evidence[0].evidence_id]
    assert hypothesis.contradicting_evidence == [result.state.evidence[1].evidence_id]
    # 第三轮是 weakened，因此终态必须回落 candidate；若投影按"最后一次出现即支持"实现，这里会读到
    # supported，报告层就会把一个已被削弱的假设当成根因输出。
    assert hypothesis.status is HypothesisStatus.CANDIDATE
    assert hypothesis.confidence == pytest.approx(0.4)
    assert [context.state.hypotheses for context in planner.contexts][0] == []
    assert len(planner.contexts[1].state.hypotheses) == 1
    assert planner.contexts[1].state.hypotheses[0].status is HypothesisStatus.CANDIDATE
    assert planner.contexts[2].state.hypotheses[0].status is HypothesisStatus.SUPPORTED
    assert planner.contexts[2].state.hypotheses[0].confidence == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_fabricated_hypothesis_reference_is_blocked_like_decision_reference() -> None:
    """验证假设更新里的引用与决策级引用共用同一道白名单门禁。

    假设更新的 evidence_refs 会成为报告根因的引用，如果这里放宽校验，模型就能用一个格式合法但
    不存在的 ID 换到一条看起来"有据可依"的结论。断言首轮被拦截、状态里不留下任何假设，并且停止
    原因是可评测的 `invalid_evidence_reference` 而不是普通结束。
    """

    decision = _with_updates(
        _batch_decision(specs=[(ToolName.LTS_GET_TASK_STATUS, "lts_synthetic_task")]),
        [
            HypothesisUpdate(
                hypothesis_id=_HYPOTHESIS_ID,
                status=HypothesisUpdateStatus.NEW,
                symptom="合成 LTS 任务连续三次调度失败。",
                candidate_root_cause="partition_date 参数格式不符合调度约定。",
                evidence_refs=["ev_not_in_state_001"],
            )
        ],
    )
    planner = ScriptedPlanner([decision])
    executor = RecordingExecutor()
    loop = BoundedReactLoop(
        planner=planner,
        executor=executor,
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    assert result.state.stop_reason == ReactStopReason.INVALID_EVIDENCE_REFERENCE.value
    assert result.state.hypotheses == []
    assert executor.actions == []


@pytest.mark.asyncio
async def test_planner_stop_reason_reaches_state_as_evaluable_enum_value() -> None:
    """验证结构化停止原因以枚举值而不是自由文本进入状态与公开事件。

    停止原因会同时进入 `run_events`、trace span 属性和 Golden 评测的分类比较，自由文本会让公开
    时间线出现接近推理过程的长篇叙述，并使"命中期望停止原因"永远无法为真。断言状态与终止事件
    携带同一个 ASCII 枚举值，且该值确实来自 `PlannerStopReason`。
    """

    planner = ScriptedPlanner(
        [_finish_decision(stop_reason=PlannerStopReason.EVIDENCE_CONFLICT_REQUIRES_MANUAL_REVIEW)]
    )
    loop = BoundedReactLoop(
        planner=planner,
        executor=RecordingExecutor(),
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    result = await loop.run(_run_request())

    expected = PlannerStopReason.EVIDENCE_CONFLICT_REQUIRES_MANUAL_REVIEW.value
    assert result.state.stop_reason == expected
    assert result.events[-1].stop_reason == expected
    assert result.events[-1].stop_reason.isascii()


def _knowledge_bundle() -> GraphEvidenceBundle:
    """构造只含一个 root_cause 知识节点的最小 GraphRAG Bundle，用于白名单同源断言。

    节点的 `evidence_id` 用生产前缀 `kn_`，因为报告层正是按这个 ID 判定引用合法性；不放路径与
    文档切片，使断言能精确区分"知识引用被接受"与"实时 Observation 才能支撑假设"这两件事。
    """

    return GraphEvidenceBundle(
        query="合成 LTS 分区参数格式问题",
        retrieval_mode=RetrievalMode.HYBRID_GRAPH,
        budget=EvidenceBundleBudget(max_bytes=4000, max_nodes=4, max_paths=2),
        used_bytes=256,
        selected_nodes=[
            BundledKnowledgeNode(
                evidence_id="kn_invalid_partition_date",
                node_id="cause_invalid_partition_date",
                node_type=KnowledgeNodeType.ROOT_CAUSE,
                name="分区参数格式不符合任务声明",
                content="合成知识：partition_date 必须使用 yyyy-MM-dd，否则启动校验直接失败。",
                source_id="synthetic_knowledge_source",
                source_span="合成知识第 1 段",
                reliability=0.8,
                retrieval_score=0.7,
            )
        ],
    )


@pytest.mark.asyncio
async def test_knowledge_reference_passes_gate_but_cannot_promote_hypothesis() -> None:
    """验证 Bundle 知识引用与报告层同源被接受，但不足以把假设升为 supported。

    Planner 侧白名单曾比报告层更窄，模型引用 Prompt 里刚给出的 `kn_*` 反而被整批拒绝，Run C 的一个
    案例即以 `invalid_evidence_reference` 终止；因此这里先断言运行正常结束、引用被保留。同时升级
    口径只认实时 Observation：只有知识依据时假设必须停在 candidate，否则模型能凭"知识库里有这种
    故障模式"直接换到一条会进入报告根因的结论，而本次运行其实没有观察到它。
    """

    knowledge_ref = "kn_invalid_partition_date"
    decision = _with_updates(
        _finish_decision(evidence_refs=[knowledge_ref]),
        [
            HypothesisUpdate(
                hypothesis_id=_HYPOTHESIS_ID,
                status=HypothesisUpdateStatus.NEW,
                symptom="合成 LTS 任务连续三次调度失败。",
                candidate_root_cause="partition_date 参数格式不符合调度约定。",
                evidence_refs=[knowledge_ref],
            )
        ],
    )
    loop = BoundedReactLoop(
        planner=ScriptedPlanner([decision]),
        executor=RecordingExecutor(),
        config=ReactLoopConfig(max_steps=6, total_timeout_seconds=2),
    )

    request = _run_request().model_copy(update={"evidence_bundle": _knowledge_bundle()})
    result = await loop.run(request)

    assert result.state.stop_reason == PlannerStopReason.EVIDENCE_SUFFICIENT.value
    assert result.state.next_action is not None
    assert result.state.next_action.evidence_refs == [knowledge_ref]
    assert [item.hypothesis_id for item in result.state.hypotheses] == [_HYPOTHESIS_ID]
    assert result.state.hypotheses[0].supporting_evidence == [knowledge_ref]
    assert result.state.hypotheses[0].status is HypothesisStatus.CANDIDATE
    assert result.state.hypotheses[0].confidence == pytest.approx(0.4)
