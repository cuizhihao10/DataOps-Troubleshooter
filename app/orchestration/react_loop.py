"""用 LangGraph 实现 capability 注入和有界 Planner ReAct Action/Observation 循环。

图只包含确定性路由、Planner 协议调用、MCP 执行和 Observation 回写。Planner 可替换但不能
直接执行工具；总超时、组件范围、trace、一致引用和同参去重由本模块客观执行。
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
from app.domain.models import Evidence, ToolEvent
from app.domain.planner import PlannerStatus, ToolAction
from app.domain.tooling import ToolName
from app.mcp.observation import ToolObservation
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
    """构建 route → planner → execute → planner 的固定 LangGraph 拓扑。

    状态 Schema 使用 Pydantic `ReactGraphState`，context 使用不可序列化依赖容器；条件边只根据
    status 和结构化 PlannerDecision 路由。编译失败会在控制器构造时暴露，不延迟到首个请求。
    """

    graph = StateGraph(ReactGraphState, context_schema=ReactGraphRuntime)
    graph.add_node("select_capabilities", _select_capabilities)
    graph.add_node("planner_react", _planner_react)
    graph.add_node("execute_tool", _execute_tool)
    graph.add_edge(START, "select_capabilities")
    graph.add_edge("select_capabilities", "planner_react")
    graph.add_conditional_edges(
        "planner_react",
        _route_after_planner,
        {"execute_tool": "execute_tool", "end": END},
    )
    graph.add_edge("execute_tool", "planner_react")
    return graph.compile(name="dataops_bounded_react_v2")


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

    节点先检查工具步数和剩余时间，再调用可替换 Planner。决策仅记录公开摘要；证据引用、工具
    组件范围、trace 和同参指纹依次校验。违规 Action 不进入 MCP 节点，并产生可解释终止事件。
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

    context = PlannerTurnContext(
        state=graph_state.agent_state,
        capabilities=selection,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
        history_case_matches=graph_state.history_case_matches,
        max_react_steps=runtime.context.config.max_steps,
        remaining_time_ms=remaining_time_ms,
    )
    try:
        decision = await runtime.context.planner.decide(context)
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
        tool_name=decision.action.tool_name if decision.action else None,
        observation_refs=tuple(decision.evidence_refs),
    )

    # Planner 引用必须来自当前状态；模型不能仅凭格式合法就创造不存在的 evidence_id/path_id。
    valid_refs = {item.evidence_id for item in agent_state.evidence} | {
        path.path_id for path in agent_state.retrieved_paths
    }
    invalid_refs = sorted(set(decision.evidence_refs) - valid_refs)
    if invalid_refs:
        return _stop_graph_state(
            updated,
            reason=ReactStopReason.INVALID_EVIDENCE_REFERENCE,
            summary=f"Planner 引用了 {len(invalid_refs)} 个当前状态中不存在的证据。",
            event_type=ReactEventType.POLICY_BLOCKED,
        )

    if decision.status is not PlannerStatus.CALL_TOOL:
        return _stop_graph_state(
            updated,
            reason=decision.stop_reason or "planner_stopped",
            summary="Planner 已选择结束调查或请求用户补充信息。",
            event_type=ReactEventType.LOOP_STOPPED,
        )

    action = decision.action
    if action is None:
        raise RuntimeError("validated call_tool decision unexpectedly lacks action")
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
    if fingerprint in graph_state.executed_action_fingerprints:
        return _stop_graph_state(
            updated,
            reason=ReactStopReason.DUPLICATE_ACTION_BLOCKED,
            summary="同一工具与规范化参数已经执行，重复 Action 未进入 MCP。",
            event_type=ReactEventType.POLICY_BLOCKED,
        )
    return updated


def _route_after_planner(graph_state: ReactGraphState) -> str:
    """根据结构化循环状态选择执行工具或结束图，不读取自然语言摘要。

    只有 running 且 next_action 为 call_tool 的状态可以进入执行节点；所有停止路径统一返回 end。
    缺少 Action 的 running 状态代表图实现错误，显式抛出 RuntimeError 防止静默结束。
    """

    if graph_state.status is ReactLoopStatus.STOPPED:
        return "end"
    decision = graph_state.agent_state.next_action
    if decision is None or decision.status is not PlannerStatus.CALL_TOOL:
        raise RuntimeError("running React graph requires a call_tool decision")
    return "execute_tool"


async def _execute_tool(
    graph_state: ReactGraphState,
    runtime: Runtime[ReactGraphRuntime],
) -> ReactGraphState:
    """跨注入执行器完成真实 MCP Action，并把 Observation 原子回写状态。

    工具执行成功或失败都追加完整 ToolEvent；Evidence 和 observation_refs 按稳定 ID 去重，
    `react_step` 只增加一次而不受执行器内部重试事件数量影响。执行器不返回则由总超时取消。
    """

    decision = graph_state.agent_state.next_action
    if decision is None or decision.action is None:
        raise RuntimeError("execute_tool requires a validated pending action")
    action = decision.action
    observation = await runtime.context.executor.execute(action)

    # 先合并全部 Observation 数据，再一次构造新 AgentState，避免其他节点看到半回写状态。
    evidence = _merge_evidence(graph_state.agent_state.evidence, observation.evidence)
    tool_events = _merge_tool_events(
        graph_state.agent_state.tool_events,
        observation.tool_events,
    )
    observation_refs = _stable_unique(
        [*graph_state.agent_state.observation_refs, *observation.observation_refs]
    )
    agent_state = graph_state.agent_state.model_copy(
        update={
            "evidence": evidence,
            "tool_events": tool_events,
            "observation_refs": observation_refs,
            "react_step": graph_state.agent_state.react_step + 1,
        }
    )
    fingerprints = _stable_unique(
        [*graph_state.executed_action_fingerprints, _action_fingerprint(action)]
    )
    if observation.response.ok:
        summary = (
            f"{action.tool_name.value} 成功，记录 {len(observation.evidence)} 条证据和 "
            f"{len(observation.tool_events)} 次尝试事件。"
        )
    else:
        error_code = (
            observation.response.error_code.value if observation.response.error_code else "UNKNOWN"
        )
        summary = (
            f"{action.tool_name.value} 失败（{error_code}），记录 "
            f"{len(observation.tool_events)} 次尝试且未伪造证据。"
        )
    updated = graph_state.model_copy(
        update={
            "agent_state": agent_state,
            "executed_action_fingerprints": fingerprints,
        }
    )
    return _append_event(
        updated,
        event_type=ReactEventType.OBSERVATION_RECORDED,
        summary=summary,
        tool_name=action.tool_name,
        observation_refs=tuple(observation.observation_refs),
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
