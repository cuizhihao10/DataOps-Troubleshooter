"""用 LangGraph 实现 capability 注入和有界 Planner ReAct Action/Observation 循环。

图只包含确定性路由、Planner 协议调用、并行 MCP 执行和 Observation 回写。Planner 可替换但不能
直接执行工具；总超时、组件范围、并行上限、trace、一致引用和同参去重由本模块客观执行。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.agents.planner import PlannerAgent, PlannerAgentError, PlannerTurnContext
from app.capabilities import CapabilityRegistry, get_capability_registry
from app.domain.models import (
    Component,
    Evidence,
    FaultHypothesis,
    HypothesisStatus,
    ToolEvent,
)
from app.domain.planner import (
    HypothesisUpdate,
    HypothesisUpdateStatus,
    PlannerStatus,
    ToolAction,
)
from app.domain.tooling import ToolName
from app.mcp.observation import ToolObservation
from app.observability.tracing import TraceSpanKind, TraceSpanStatus, trace_span
from app.orchestration.models import (
    REACT_LOOP_CONTRACT_ID,
    ReactEventType,
    ReactGraphState,
    ReactLoopConfig,
    ReactLoopStatus,
    ReactPublicEvent,
    ReactRunRequest,
    ReactRunResult,
    ReactStopReason,
)
from app.reporting.evidence import collect_valid_reference_ids


class ToolActionExecutor(Protocol):
    """声明 LangGraph 工具节点依赖的最小异步执行接口。

    真实 `McpToolExecutor` 和测试替身都可满足该协议；返回值必须是已经标准化的 ToolObservation，
    因而编排层无需接触 MCP SDK 对象或 Fixture，也不能绕过 Evidence/ToolEvent 边界。
    """

    async def execute(self, action: ToolAction) -> ToolObservation:
        """执行一个已通过 Planner Schema 与策略门禁的只读 ToolAction。

        输入必须包含白名单工具与统一请求，输出包含终态响应、证据和全部重试事件。实现异常由
        总超时或上层错误边界处理，不得返回松散字典或吞掉失败。
        """

        ...


@dataclass(frozen=True, slots=True)
class ReactGraphRuntime:
    """保存一次图执行共享但不进入 checkpoint 的依赖和绝对截止时间。

    Planner、执行器和注册表是进程内对象，不能序列化进领域状态；LangGraph context 将它们与
    Pydantic 状态分离。每次 run 创建独立 context，因此并发诊断不会共享截止时间或可变状态。
    """

    planner: PlannerAgent
    executor: ToolActionExecutor
    registry: CapabilityRegistry
    config: ReactLoopConfig
    deadline_monotonic: float


class BoundedReactLoop:
    """编译并运行固定拓扑的 LangGraph Planner ReAct 控制器。

    构造时注入 Planner、工具执行器、预算和固定 capability 注册表；`run` 为每次调用创建独立
    runtime context，并通过流式状态保存最后完成节点，使总超时也能返回已有证据而非回滚历史。
    """

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        executor: ToolActionExecutor,
        config: ReactLoopConfig,
        registry: CapabilityRegistry | None = None,
    ) -> None:
        """保存可替换边界并一次编译 route/planner/execute 固定图。

        构造不会调用模型、MCP 或数据库；图拓扑可在多个运行间复用，而 runtime context 每次隔离。
        registry 缺省使用已启动审计的固定五能力实现，不接受动态 capability 定义。
        """

        self._planner = planner
        self._executor = executor
        self._config = config
        self._registry = registry or get_capability_registry()
        self._graph = _build_react_graph()

    async def run(self, request: ReactRunRequest) -> ReactRunResult:
        """执行有界 LangGraph 循环，并在所有正常或安全降级路径返回终态结果。

        输入可包含已有 ToolEvent，控制器会重建 Action 指纹防止恢复后重复调用。图状态以 values
        流逐节点保存；总超时取消正在运行的 Planner/MCP 节点，并基于最后完整状态追加公开终止
        事件。未预期的编程异常不吞掉，便于测试和启动环境发现真实缺陷。
        """

        initial_state = ReactGraphState(
            agent_state=request.state,
            capability_request=request.capability_request,
            evidence_bundle=request.evidence_bundle,
            confirmed_case_memories=request.confirmed_case_memories,
            history_case_matches=request.history_case_matches,
            executed_action_fingerprints=_fingerprints_from_tool_events(request.state.tool_events),
        )
        runtime_context = ReactGraphRuntime(
            planner=self._planner,
            executor=self._executor,
            registry=self._registry,
            config=self._config,
            deadline_monotonic=monotonic() + self._config.total_timeout_seconds,
        )
        latest_state = initial_state

        try:
            # 外层墙钟预算覆盖 Planner 和 MCP 的等待时间；astream 让已完成节点状态持续可恢复。
            async with asyncio.timeout(self._config.total_timeout_seconds):
                async for raw_state in self._graph.astream(
                    initial_state,
                    context=runtime_context,
                    stream_mode="values",
                    config={"recursion_limit": self._config.max_steps * 2 + 6},
                ):
                    latest_state = ReactGraphState.model_validate(raw_state)
        except TimeoutError:
            latest_state = _stop_graph_state(
                latest_state,
                reason=ReactStopReason.TOTAL_TIMEOUT,
                summary="ReAct 总墙钟预算已耗尽，正在运行的 Planner 或工具节点已取消。",
                event_type=ReactEventType.LOOP_STOPPED,
            )

        if latest_state.capability_selection is None:
            raise RuntimeError("React graph ended before capability selection")
        return ReactRunResult(
            contract_id=REACT_LOOP_CONTRACT_ID,
            state=latest_state.agent_state,
            capabilities=latest_state.capability_selection,
            events=latest_state.events,
        )


def _build_react_graph():
    """构建 route → planner → execute_tools → planner 的固定 LangGraph 拓扑。

    状态 Schema 使用 Pydantic `ReactGraphState`，context 使用不可序列化依赖容器；条件边只根据
    status 和结构化 PlannerDecision 路由。执行节点一次处理整批并行 Action，因此并行度不需要引入
    LangGraph 的 fan-out 边——那会把重复检测和预算记账分散到多个分支里。编译失败会在控制器构造时
    暴露，不延迟到首个请求。
    """

    graph = StateGraph(ReactGraphState, context_schema=ReactGraphRuntime)
    graph.add_node("select_capabilities", _select_capabilities)
    graph.add_node("planner_react", _planner_react)
    graph.add_node("execute_tools", _execute_tools)
    graph.add_edge(START, "select_capabilities")
    graph.add_edge("select_capabilities", "planner_react")
    graph.add_conditional_edges(
        "planner_react",
        _route_after_planner,
        {"execute_tools": "execute_tools", "end": END},
    )
    graph.add_edge("execute_tools", "planner_react")
    return graph.compile(name="dataops_bounded_react_v3")


async def _select_capabilities(
    graph_state: ReactGraphState,
    runtime: Runtime[ReactGraphRuntime],
) -> ReactGraphState:
    """选择固定 capability 组合并注入 AgentState 的意图与活动名称。

    节点输入/输出均为 Pydantic 模型；注册表只执行确定性校验，不调用模型。旧 stop_reason 和
    next_action 会清空以开始本轮运行，但已有证据、路径和工具事件保持不变供恢复场景使用。
    """

    selection = runtime.context.registry.select(graph_state.capability_request)
    agent_state = graph_state.agent_state.model_copy(
        update={
            "intent": selection.intent.value,
            "active_capabilities": [name.value for name in selection.active_capabilities],
            "next_action": None,
            "stop_reason": None,
        }
    )
    updated = graph_state.model_copy(
        update={
            "agent_state": agent_state,
            "capability_selection": selection,
            "status": ReactLoopStatus.RUNNING,
        }
    )
    return _append_event(
        updated,
        event_type=ReactEventType.CAPABILITIES_SELECTED,
        summary=(
            f"已按 {selection.intent.value} 选择 {len(selection.active_capabilities)} 项固定能力。"
        ),
    )


async def _planner_react(
    graph_state: ReactGraphState,
    runtime: Runtime[ReactGraphRuntime],
) -> ReactGraphState:
    """执行一轮 Planner 决策，并在任何外部 Action 前应用确定性策略门禁。

    节点先检查工具步数和剩余时间，再调用可替换 Planner。决策仅记录公开摘要；随后依次校验证据
    引用、并行批次大小、剩余步数、工具组件范围、trace 和同参指纹。任何一条不通过就整批拒绝而
    不是悄悄截断，因为"只执行了你要求的一部分"会让 Planner 基于不完整前提继续推理。
    """

    if graph_state.agent_state.react_step >= runtime.context.config.max_steps:
        return _stop_graph_state(
            graph_state,
            reason=ReactStopReason.REACT_BUDGET_EXHAUSTED,
            summary="已达到 Planner 工具 Action 上限，循环在再次调用模型前停止。",
            event_type=ReactEventType.LOOP_STOPPED,
        )

    selection = graph_state.capability_selection
    if selection is None:
        raise RuntimeError("planner node requires capability selection")
    remaining_time_ms = max(
        0,
        int((runtime.context.deadline_monotonic - monotonic()) * 1000),
    )
    if remaining_time_ms == 0:
        return _stop_graph_state(
            graph_state,
            reason=ReactStopReason.TOTAL_TIMEOUT,
            summary="Planner 调用前检测到总墙钟预算已耗尽。",
            event_type=ReactEventType.LOOP_STOPPED,
        )

    # 剩余步数同时进入 Prompt 和并行上限：模型看到的可并行数量必须等于控制器真正允许的数量，
    # 否则它会反复提交刚好超预算的批次，而每次拒绝都白花一次模型调用。
    remaining_tool_calls = runtime.context.config.max_steps - graph_state.agent_state.react_step
    context = PlannerTurnContext(
        state=graph_state.agent_state,
        capabilities=selection,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
        history_case_matches=graph_state.history_case_matches,
        max_react_steps=runtime.context.config.max_steps,
        max_parallel_actions=min(
            runtime.context.config.max_parallel_actions,
            remaining_tool_calls,
        ),
        remaining_time_ms=remaining_time_ms,
    )
    try:
        # span 只包住模型往返：门禁判定属于确定性逻辑，混进来会让 Planner 延迟看起来比实际更高。
        with trace_span(
            TraceSpanKind.REACT_STEP,
            "react.planner_decision",
            react_step=graph_state.agent_state.react_step,
            remaining_time_ms=remaining_time_ms,
        ) as span:
            decision = await runtime.context.planner.decide(context)
            span.annotate(
                decision_status=decision.status.value,
                action_count=len(decision.actions),
                evidence_ref_count=len(decision.evidence_refs),
            )
    except PlannerAgentError as exc:
        # 只把适配层已净化的预期失败转换成终态；编程异常继续传播，避免隐藏真实缺陷。
        return _stop_graph_state(
            graph_state,
            reason=exc.stop_reason,
            summary=exc.public_summary,
            event_type=ReactEventType.LOOP_STOPPED,
        )
    agent_state = graph_state.agent_state.model_copy(update={"next_action": decision})
    updated = _append_event(
        graph_state.model_copy(update={"agent_state": agent_state}),
        event_type=ReactEventType.PLANNER_DECISION,
        summary=decision.decision_summary,
        # 单 Action 保留具体工具名；批次刻意留空 tool_name，因为只写第一个工具会让时间线读起来
        # 像"只调用了一个工具"，批次规模统一由 parallel_action_count 表达。
        tool_name=(decision.actions[0].tool_name if len(decision.actions) == 1 else None),
        parallel_action_count=len(decision.actions),
        observation_refs=tuple(decision.evidence_refs),
    )

    # Planner 引用必须来自当前状态；模型不能仅凭格式合法就创造不存在的 evidence_id/path_id。
    # 假设更新里的引用走同一道门禁：它们会成为报告根因的 evidence_refs，若放宽校验，模型就能
    # 用编造的引用换到一条看起来"有据可依"的结论。可引用宇宙必须与报告层完全同源：草稿、策略
    # 校验、修订和 Auditor 都接受 Bundle 的 kn_*/path_*/dc_* 与 confirmed 案例 ID，早期版本只认
    # 实时 evidence_id 与 checkpoint 旧路径，于是模型引用 Prompt 里明明给出的知识证据反而被整批
    # 拒绝——首次真实模型评测第三个案例的 invalid_evidence_reference 就是这条口径错误。
    valid_refs = collect_valid_reference_ids(
        agent_state,
        graph_state.evidence_bundle,
        graph_state.confirmed_case_memories,
    )
    claimed_refs = set(decision.evidence_refs)
    for update in decision.hypothesis_updates:
        claimed_refs.update(update.evidence_refs)
    invalid_refs = sorted(claimed_refs - valid_refs)
    if invalid_refs:
        return _stop_graph_state(
            updated,
            reason=ReactStopReason.INVALID_EVIDENCE_REFERENCE,
            summary=f"Planner 引用了 {len(invalid_refs)} 个当前状态中不存在的证据。",
            event_type=ReactEventType.POLICY_BLOCKED,
        )

    # 假设投影必须发生在这里而不是报告层：确定性草稿只认 AgentState.hypotheses，早期版本把
    # hypothesis_updates 连同决策一起丢掉，于是模型在 decision_summary 里说出了正确根因，报告的
    # root_causes 却恒为空，Auditor 随后以 report_incomplete 否决——首次真实模型评测里
    # root_cause_top1_hit_rate 实测为 0 就是这条链路造成的，与模型能力无关。
    agent_state = agent_state.model_copy(
        update={
            "hypotheses": _project_hypothesis_updates(
                agent_state.hypotheses,
                decision.hypothesis_updates,
                components=list(selection.components),
                # 升为 supported 只认实时 Observation：知识节点与历史案例可以被引用，但"知识库里
                # 有这种故障模式"不等于"本次运行观察到了它"，否则模型能凭检索结果直接换到根因。
                observation_refs={item.evidence_id for item in agent_state.evidence},
            )
        }
    )
    updated = updated.model_copy(update={"agent_state": agent_state})

    if decision.status is not PlannerStatus.CALL_TOOL:
        return _stop_graph_state(
            updated,
            reason=decision.stop_reason.value if decision.stop_reason else "planner_stopped",
            summary="Planner 已选择结束调查或请求用户补充信息。",
            event_type=ReactEventType.LOOP_STOPPED,
        )

    actions = list(decision.actions)
    if not actions:
        raise RuntimeError("validated call_tool decision unexpectedly lacks actions")
    if len(actions) > runtime.context.config.max_parallel_actions:
        return _stop_graph_state(
            updated,
            reason=ReactStopReason.PARALLEL_LIMIT_EXCEEDED,
            summary=(
                f"Planner 提交了 {len(actions)} 个并行 Action，超过本次运行的并行上限 "
                f"{runtime.context.config.max_parallel_actions}。"
            ),
            event_type=ReactEventType.POLICY_BLOCKED,
        )
    if len(actions) > remaining_tool_calls:
        # 并行只压缩等待时间，不额外发放取证预算；批次超出剩余步数时整批拒绝，避免"执行两个、
        # 丢弃一个"这种让 Planner 无法解释的部分成功。
        return _stop_graph_state(
            updated,
            reason=ReactStopReason.PARALLEL_BUDGET_EXCEEDED,
            summary=(
                f"Planner 提交的 {len(actions)} 个并行 Action 超过剩余 "
                f"{remaining_tool_calls} 个工具步数预算。"
            ),
            event_type=ReactEventType.POLICY_BLOCKED,
        )

    batch_fingerprints: list[str] = []
    for action in actions:
        if action.tool_name not in selection.tool_priority:
            return _stop_graph_state(
                updated,
                reason=ReactStopReason.TOOL_NOT_ALLOWED_BY_CAPABILITY,
                summary="Planner 选择的工具不属于当前已批准组件范围。",
                event_type=ReactEventType.POLICY_BLOCKED,
            )
        if action.arguments.trace_id != agent_state.run_id:
            return _stop_graph_state(
                updated,
                reason=ReactStopReason.TRACE_ID_MISMATCH,
                summary="ToolAction trace_id 与当前 run_id 不一致，调用已拦截。",
                event_type=ReactEventType.POLICY_BLOCKED,
            )
        fingerprint = _action_fingerprint(action)
        # 批内重复与历史重复用同一套指纹判定：并行不应成为"同一次查询同时发三遍"的绕过路径。
        if fingerprint in graph_state.executed_action_fingerprints or (
            fingerprint in batch_fingerprints
        ):
            return _stop_graph_state(
                updated,
                reason=ReactStopReason.DUPLICATE_ACTION_BLOCKED,
                summary="同一工具与规范化参数已经执行或在同批次内重复，重复 Action 未进入 MCP。",
                event_type=ReactEventType.POLICY_BLOCKED,
            )
        batch_fingerprints.append(fingerprint)
    return updated


# 置信度由状态确定性映射，而不是让模型自报一个数字：报告层会把它渲染成 RootCauseConclusion 的
# confidence，一旦交给模型，读者看到的"0.92"既无法复算也无法反驳。CONFIRMED 不在表内，它只能
# 由用户确认案例记忆时产生，Planner 无权自我确认。
_HYPOTHESIS_CONFIDENCE: dict[HypothesisStatus, float] = {
    HypothesisStatus.CANDIDATE: 0.4,
    HypothesisStatus.SUPPORTED: 0.7,
    HypothesisStatus.REJECTED: 0.0,
}


def _projected_hypothesis_status(
    update_status: HypothesisUpdateStatus,
    *,
    supporting_count: int,
) -> HypothesisStatus:
    """把 Planner 的假设更新语义映射成受证据数量约束的领域假设状态。

    `new`/`strengthened` 只有在累计到至少一条实时 Observation 引用时才能升为 supported，否则停在
    candidate；这样"模型宣称已被证实"就无法脱离本次运行真正看到的事实，也无法只凭知识库命中或
    历史案例换到根因。`weakened` 一律回落 candidate 而不是直接拒绝，保留后续轮次重新加强的可能；
    `rejected` 直接终止该假设，报告层不会再引用它。
    """

    if update_status is HypothesisUpdateStatus.REJECTED:
        return HypothesisStatus.REJECTED
    if update_status is HypothesisUpdateStatus.WEAKENED:
        return HypothesisStatus.CANDIDATE
    if supporting_count > 0:
        return HypothesisStatus.SUPPORTED
    return HypothesisStatus.CANDIDATE


def _project_hypothesis_updates(
    existing: list[FaultHypothesis],
    updates: list[HypothesisUpdate],
    *,
    components: list[Component],
    observation_refs: set[str],
) -> list[FaultHypothesis]:
    """把本轮结构化假设更新确定性合并进已有假设集合，保持顺序与引用累积。

    组件范围一律取本次运行已批准的 capability 组件，模型不能在假设里自述组件，避免报告把未获批
    组件写进结论。引用按状态分流进支持或反对集合并去重累积，因此多轮取证会持续增强同一假设而不是
    互相覆盖；`observation_refs` 是本次运行的实时 evidence_id 集合，只有它计入升级为 supported 的
    支持数量，知识与历史引用仍会保留在 supporting_evidence 里供报告溯源。更新引用了不存在的
    hypothesis_id 且状态不是 `new` 时直接忽略：静默创建会让"增强"凭空变成一条没有症状描述的新
    结论，而抛错会让一次拼错 ID 毁掉整轮真实取证。
    """

    projected = {item.hypothesis_id: item for item in existing}
    order = [item.hypothesis_id for item in existing]
    for update in updates:
        current = projected.get(update.hypothesis_id)
        symptom = (update.symptom or "").strip()
        root_cause = (update.candidate_root_cause or "").strip()
        if current is None:
            if update.status is not HypothesisUpdateStatus.NEW:
                continue
            current = FaultHypothesis(
                hypothesis_id=update.hypothesis_id,
                symptom=symptom,
                candidate_root_cause=root_cause,
                components=list(components),
            )
            order.append(update.hypothesis_id)
        supporting = list(current.supporting_evidence)
        contradicting = list(current.contradicting_evidence)
        target = (
            contradicting
            if update.status
            in {HypothesisUpdateStatus.WEAKENED, HypothesisUpdateStatus.REJECTED}
            else supporting
        )
        for ref in update.evidence_refs:
            if ref not in target:
                target.append(ref)
        status = _projected_hypothesis_status(
            update.status,
            supporting_count=len([ref for ref in supporting if ref in observation_refs]),
        )
        projected[update.hypothesis_id] = current.model_copy(
            update={
                "symptom": symptom or current.symptom,
                "candidate_root_cause": root_cause or current.candidate_root_cause,
                "supporting_evidence": supporting,
                "contradicting_evidence": contradicting,
                "status": status,
                "confidence": _HYPOTHESIS_CONFIDENCE[status],
            }
        )
    return [projected[hypothesis_id] for hypothesis_id in order]


def _route_after_planner(graph_state: ReactGraphState) -> str:
    """根据结构化循环状态选择执行工具批次或结束图，不读取自然语言摘要。

    只有 running 且 next_action 为 call_tool 的状态可以进入执行节点；所有停止路径统一返回 end。
    缺少 Action 的 running 状态代表图实现错误，显式抛出 RuntimeError 防止静默结束。
    """

    if graph_state.status is ReactLoopStatus.STOPPED:
        return "end"
    decision = graph_state.agent_state.next_action
    if decision is None or decision.status is not PlannerStatus.CALL_TOOL:
        raise RuntimeError("running React graph requires a call_tool decision")
    return "execute_tools"


async def _execute_tools(
    graph_state: ReactGraphState,
    runtime: Runtime[ReactGraphRuntime],
) -> ReactGraphState:
    """并发执行本轮整批只读 MCP Action，并把全部 Observation 原子回写状态。

    批次用 `asyncio.gather` 同时发起：九个工具都是只读的，且两种传输都不共享会话状态——stdio 每次
    调用起一个独立子进程，Streamable HTTP 共享 httpx 连接池但每次 `call_tool` 新建 MCP 会话——因此
    并发调用之间没有共享连接或游标可被破坏。`return_exceptions=True` 让编程异常
    在所有兄弟协程收尾后再原样重抛，避免第一个失败留下仍在写 span 的孤儿任务。回写按 Action 顺序
    进行，`react_step` 增加批次长度，因此并行只买到更低延迟而不是更多取证预算。
    """

    decision = graph_state.agent_state.next_action
    if decision is None or not decision.actions:
        raise RuntimeError("execute_tools requires a validated pending action batch")
    actions = list(decision.actions)
    # 批 span 是父节点，每个 Action 的 react.tool_call 是子节点：只有这样火焰图才能同时显示
    # "整批等了多久"和"哪个工具是长尾"，而把三个 span 平铺会让并行看起来像串行。
    with trace_span(
        TraceSpanKind.TOOL_CALL,
        "react.tool_batch",
        action_count=len(actions),
        tool_names="+".join(action.tool_name.value for action in actions),
        react_step=graph_state.agent_state.react_step,
    ) as batch_span:
        results = await asyncio.gather(
            *(
                _execute_single_action(
                    runtime.context.executor,
                    action,
                    react_step=graph_state.agent_state.react_step,
                )
                for action in actions
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        observations = [result for result in results if isinstance(result, ToolObservation)]
        failed_count = sum(1 for item in observations if not item.response.ok)
        batch_span.annotate(ok_count=len(observations) - failed_count, failed_count=failed_count)
        if failed_count:
            batch_span.mark(TraceSpanStatus.ERROR)

    # 先合并全部 Observation 数据，再一次构造新 AgentState，避免其他节点看到半回写状态。
    evidence = list(graph_state.agent_state.evidence)
    tool_events = list(graph_state.agent_state.tool_events)
    observation_refs = list(graph_state.agent_state.observation_refs)
    fingerprints = list(graph_state.executed_action_fingerprints)
    for action, observation in zip(actions, observations, strict=True):
        evidence = _merge_evidence(evidence, observation.evidence)
        tool_events = _merge_tool_events(tool_events, observation.tool_events)
        observation_refs = _stable_unique([*observation_refs, *observation.observation_refs])
        fingerprints = _stable_unique([*fingerprints, _action_fingerprint(action)])
    agent_state = graph_state.agent_state.model_copy(
        update={
            "evidence": evidence,
            "tool_events": tool_events,
            "observation_refs": observation_refs,
            "react_step": graph_state.agent_state.react_step + len(actions),
        }
    )
    updated = graph_state.model_copy(
        update={
            "agent_state": agent_state,
            "executed_action_fingerprints": fingerprints,
        }
    )
    # 每个 Action 单独产生一条 Observation 事件：批次内某个工具失败必须能被单独读出来，
    # 否则"三个里有一个 EMPTY_RESULT"会被压缩成一句无法追责的批次摘要。
    for action, observation in zip(actions, observations, strict=True):
        updated = _append_event(
            updated,
            event_type=ReactEventType.OBSERVATION_RECORDED,
            summary=_observation_summary(action, observation),
            tool_name=action.tool_name,
            parallel_action_count=len(actions),
            observation_refs=tuple(observation.observation_refs),
        )
    return updated


async def _execute_single_action(
    executor: ToolActionExecutor,
    action: ToolAction,
    *,
    react_step: int,
) -> ToolObservation:
    """在独立子 span 内执行批次中的一个 Action，并返回标准化 Observation。

    span 包住整个 Action（含执行器内部重试），因此 P95 指标反映 Planner 真正等待的墙钟时间。
    父指针取自 ContextVar，而 `asyncio.gather` 为每个协程复制当前上下文，因此批内子 span 会稳定
    挂在批 span 之下，互相之间不会因为完成顺序不同而错挂。
    """

    with trace_span(
        TraceSpanKind.TOOL_CALL,
        "react.tool_call",
        tool_name=action.tool_name.value,
        react_step=react_step,
    ) as span:
        observation = await executor.execute(action)
        span.annotate(
            ok=observation.response.ok,
            error_code=(
                observation.response.error_code.value if observation.response.error_code else "none"
            ),
            attempt_count=len(observation.tool_events),
            evidence_count=len(observation.evidence),
        )
        if not observation.response.ok:
            span.mark(TraceSpanStatus.ERROR)
    return observation


def _observation_summary(action: ToolAction, observation: ToolObservation) -> str:
    """生成一条只描述工具结果规模的公开 Observation 摘要。

    摘要刻意只包含工具名、证据条数、尝试次数和错误码：这些都是可以核对的事实，而把响应内容
    摘进事件会让时间线变成第二份未经校验的证据来源。失败路径明确声明未伪造证据。
    """

    if observation.response.ok:
        return (
            f"{action.tool_name.value} 成功，记录 {len(observation.evidence)} 条证据和 "
            f"{len(observation.tool_events)} 次尝试事件。"
        )
    error_code = (
        observation.response.error_code.value if observation.response.error_code else "UNKNOWN"
    )
    return (
        f"{action.tool_name.value} 失败（{error_code}），记录 "
        f"{len(observation.tool_events)} 次尝试且未伪造证据。"
    )


def _stop_graph_state(
    graph_state: ReactGraphState,
    *,
    reason: ReactStopReason | str,
    summary: str,
    event_type: ReactEventType,
) -> ReactGraphState:
    """把任意运行态原子转换为带公开原因和终止事件的停止态。

    枚举原因按值写入，Planner 自主原因保留字符串；AgentState 和图 status 同时更新，避免条件边
    与 API 观察不一致。调用方必须传终止类事件，事件模型会再次校验该不变量。
    """

    reason_value = reason.value if isinstance(reason, ReactStopReason) else reason
    agent_state = graph_state.agent_state.model_copy(update={"stop_reason": reason_value})
    stopped = graph_state.model_copy(
        update={"agent_state": agent_state, "status": ReactLoopStatus.STOPPED}
    )
    return _append_event(
        stopped,
        event_type=event_type,
        summary=summary,
        stop_reason=reason_value,
    )


def _append_event(
    graph_state: ReactGraphState,
    *,
    event_type: ReactEventType,
    summary: str,
    tool_name: ToolName | None = None,
    parallel_action_count: int = 0,
    observation_refs: tuple[str, ...] = (),
    stop_reason: str | None = None,
) -> ReactGraphState:
    """按单调序号生成稳定事件 ID，并返回包含新不可变事件的图状态副本。

    事件 ID 由 run_id、序号和类型计算，重放相同控制流可得到相同引用。函数不修改原列表，避免
    LangGraph 并发或调试快照之间共享可变对象；事件字段最终由 ReactPublicEvent 再校验。
    """

    sequence = len(graph_state.events) + 1
    event_id = _stable_id(
        "react_evt",
        graph_state.agent_state.run_id,
        str(sequence),
        event_type.value,
    )
    event = ReactPublicEvent(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        summary=summary,
        tool_name=tool_name,
        parallel_action_count=parallel_action_count,
        observation_refs=observation_refs,
        stop_reason=stop_reason,
    )
    return graph_state.model_copy(update={"events": [*graph_state.events, event]})


def _action_fingerprint(action: ToolAction) -> str:
    """把工具名和除 trace 外的规范化参数转换为跨 checkpoint 稳定指纹。

    ``trace_id`` 是每个新 run 必须变化的审计身份，不属于查询语义；先移除它，才能在恢复上一轮
    ToolEvent 后识别相同工具、资源、时间窗和场景。JSON 规范化避免键序/空格漏检，SHA-256 只做
    本地等价性，不承载凭据或安全签名。
    """

    payload_data = action.model_dump(mode="json")
    # trace 仍由前置门禁严格绑定当前 run_id；这里只排除它，防止新 run ID 成为重复调用绕过路径。
    payload_data["arguments"].pop("trace_id")
    payload = json.dumps(
        payload_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _fingerprints_from_tool_events(tool_events: list[ToolEvent]) -> list[str]:
    """从已有 ToolEvent 重建去重集合，使 checkpoint 恢复后仍拦截同参 Action。

    MCP 重试会产生多个具有相同工具和请求的事件，最终通过稳定去重只保留一个指纹；该过程不
    依赖 event_id，因此兼容旧事件 ID 生成规则，并保留首次出现顺序便于调试。
    """

    return _stable_unique(
        [
            _action_fingerprint(ToolAction(tool_name=event.tool_name, arguments=event.request))
            for event in tool_events
        ]
    )


def _merge_evidence(existing: list[Evidence], incoming: list[Evidence]) -> list[Evidence]:
    """按 evidence_id 合并 Observation 证据，并拒绝相同 ID 的内容漂移。

    完全相同的重放只保留首项；若 ID 相同但结构不同，说明稳定 ID 或上游来源契约冲突，函数
    抛出 ValueError 而不是覆盖旧事实。该异常属于实现/协议缺陷，不应伪装成安全降级结论。
    """

    by_id = {item.evidence_id: item for item in existing}
    for item in incoming:
        current = by_id.get(item.evidence_id)
        if current is not None and current != item:
            raise ValueError(f"conflicting Evidence payload for {item.evidence_id}")
        by_id.setdefault(item.evidence_id, item)
    return list(by_id.values())


def _merge_tool_events(existing: list[ToolEvent], incoming: list[ToolEvent]) -> list[ToolEvent]:
    """按 event_id 合并工具审计事件，并拒绝 ID 相同但载荷不同的冲突。

    合法重放不会重复污染时间线；冲突表明事件寻址不足或协议返回漂移，必须显式失败。返回顺序
    保持既有事件在前、新事件在后，使 API 时间线与真实执行顺序一致。
    """

    by_id = {item.event_id: item for item in existing}
    for item in incoming:
        current = by_id.get(item.event_id)
        if current is not None and current != item:
            raise ValueError(f"conflicting ToolEvent payload for {item.event_id}")
        by_id.setdefault(item.event_id, item)
    return list(by_id.values())


def _stable_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重字符串列表，供指纹和引用合并共享。

    集合只负责成员检测，结果列表保留时间/优先级顺序；输入输出都是新列表，不会修改调用方状态。
    字符串天然可哈希，若未来需要复杂对象应建立显式稳定键而不是隐式转换。
    """

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    """以 SHA-256 规范部件生成适合公开事件引用的 16 位稳定 ID。

    分隔符避免部件简单拼接歧义，前缀隔离事件命名空间；截断只用于作品规模的可读审计引用，
    不用于认证、加密或全局安全唯一性。相同运行和事件顺序重放时 ID 保持一致。
    """

    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
