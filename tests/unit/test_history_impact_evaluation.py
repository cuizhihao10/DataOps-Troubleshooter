"""验证 Memory off/on 端到端历史影响评测的 Schema、指标和安全失败语义。

单元测试用强类型合成 ``DiagnosisRunResult`` 隔离 LangGraph、MCP 和数据库，只验证评测器本身：
必要 Action 覆盖、意外 Action、根因稳定、TOOL 引用、历史投影、冲突提示和对照组污染门禁。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
    get_capability_registry,
)
from app.domain.models import (
    AgentState,
    AuditResult,
    AuditStatus,
    CaseMemory,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
    RootCauseConclusion,
    ToolEvent,
)
from app.domain.tooling import McpToolRequest, McpToolResponse, TimeRange, ToolName
from app.memory.matcher import explain_case_matches
from app.memory.models import (
    CaseMemoryMatch,
    MemoryRetrievalChannel,
    MemoryStageResult,
    MemoryStageStatus,
)
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    DIAGNOSIS_WORKFLOW_CONTRACT_ID,
    REACT_LOOP_CONTRACT_ID,
    DiagnosisRunResult,
    HistoryImpactEvalCase,
    HistoryImpactEvalSuite,
    HistoryImpactMode,
    ReactEventType,
    ReactPublicEvent,
    ReactRunResult,
    ReportEventType,
    ReportPublicEvent,
    ReportRunResult,
    ReportWorkflowOutcome,
    evaluate_history_impact,
    load_history_impact_eval_suite,
)

SUITE_PATH = Path("data/evals/history_impact_cases.json")
NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


class ScriptedHistoryImpactRunner:
    """按案例和模式返回结构化合成诊断结果，并支持故意污染安全边界。

    正常脚本让 action-guidance 案例只在 memory-on 选择必要工具，其余案例两组动作相同；冲突案例
    召回不同根因但报告保持本次根因。开关用于证明评测器会拒绝错误 trigger 或发现缺失冲突提示。
    """

    def __init__(
        self,
        *,
        leak_memory_off_trigger: bool = False,
        remove_conflict_warning: bool = False,
    ) -> None:
        """保存故障注入配置并初始化空调用记录。

        构造不运行评测或创建结果；每次 ``run`` 都根据实际 case/mode 新建 Pydantic 对象，防止测试
        之间共享可变状态。两个开关默认关闭，启用后只改变被测门禁对应字段。
        """

        self._leak_memory_off_trigger = leak_memory_off_trigger
        self._remove_conflict_warning = remove_conflict_warning
        self.calls: list[tuple[str, HistoryImpactMode]] = []

    async def run(
        self,
        case: HistoryImpactEvalCase,
        *,
        mode: HistoryImpactMode,
    ) -> DiagnosisRunResult:
        """记录调用并返回满足或故意违反指定评测不变量的完整诊断结果。

        memory-off trigger 污染通过返回一个本身合法的 user_requested 结果实现，使错误由 paired
        evaluator 而不是 DiagnosisRunResult Schema 检出。其他异常不会被吞掉或降级。
        """

        self.calls.append((case.case_id, mode))
        effective_mode = mode
        trigger_override = None
        if mode is HistoryImpactMode.MEMORY_OFF and self._leak_memory_off_trigger:
            effective_mode = HistoryImpactMode.MEMORY_ON
            trigger_override = HistoryTrigger.USER_REQUESTED
        return _diagnosis_result(
            case,
            mode=effective_mode,
            trigger_override=trigger_override,
            remove_conflict_warning=self._remove_conflict_warning,
        )


def test_history_impact_suite_loads_three_cases_and_rejects_component_tool_drift() -> None:
    """确认 v1 JSON 含三条案例和冲突覆盖，并拒绝工具跨越声明组件。

    测试复制 JSON 后把 LTS 案例必要工具替换为 BDS 工具；Schema 必须在任何 runner 调用前失败，
    避免 capability 越界被错误统计成 Planner 行为问题。
    """

    suite = load_history_impact_eval_suite(SUITE_PATH)

    assert suite.contract_id == "history-impact-eval:v1"
    assert len(suite.cases) == 3
    assert sum(case.expect_history_conflict for case in suite.cases) == 1

    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["required_tool_names"] = ["bds.get_task_log"]
    with pytest.raises(ValidationError, match="declared components"):
        HistoryImpactEvalSuite.model_validate(payload)


@pytest.mark.asyncio
async def test_history_impact_report_measures_action_gain_and_realtime_safety() -> None:
    """验证三案例 macro 行为增益、根因稳定、历史投影和冲突保护均按公式计算。

    off 组只有两例命中必要工具且一例执行意外工具，因此 Action 覆盖为 2/3、意外率为 1/3；on 组
    两项分别为 1 和 0。两组根因/实时引用均为 1，冲突案例明确禁止复用旧方案。
    """

    suite = load_history_impact_eval_suite(SUITE_PATH)
    runner = ScriptedHistoryImpactRunner()

    report = await evaluate_history_impact(suite, runner)

    assert report.metric_kind == "measured"
    assert report.memory_off_macro_action_coverage == pytest.approx(2 / 3)
    assert report.memory_on_macro_action_coverage == 1
    assert report.action_coverage_delta == pytest.approx(1 / 3)
    assert report.memory_off_macro_unexpected_action_rate == pytest.approx(1 / 3)
    assert report.memory_on_macro_unexpected_action_rate == 0
    assert report.unexpected_action_rate_delta == pytest.approx(-1 / 3)
    assert report.memory_off_root_cause_hit_rate == 1
    assert report.memory_on_root_cause_hit_rate == 1
    assert report.memory_off_realtime_citation_rate == 1
    assert report.memory_on_realtime_citation_rate == 1
    assert report.history_projection_pass_rate == 1
    assert report.conflict_guard_pass_rate == 1
    assert report.action_regression_count == 0
    assert report.realtime_priority_failure_count == 0
    assert len(runner.calls) == 6


@pytest.mark.asyncio
async def test_history_impact_eval_rejects_memory_off_trigger_pollution() -> None:
    """确认 off 对照组若实际启用历史召回，评测立即失败而不是报告伪增益。

    污染结果本身通过 DiagnosisRunResult 校验，但其 trigger 与请求的 memory-off 模式冲突；异常在
    第一条 paired result 校验中抛出，不会继续计算后续案例平均值。
    """

    suite = load_history_impact_eval_suite(SUITE_PATH)
    runner = ScriptedHistoryImpactRunner(leak_memory_off_trigger=True)

    with pytest.raises(ValueError, match="must use not_requested"):
        await evaluate_history_impact(suite, runner)


@pytest.mark.asyncio
async def test_history_impact_eval_counts_missing_conflict_warning_as_priority_failure() -> None:
    """确认冲突案例缺少“禁止直接复用”提示时安全通过率下降并计入优先级失败。

    根因仍保持实时值且引用完整，但历史差异说明不完整不能被视为安全使用；评测应给出 conflict
    pass rate 0，并把该案例计入 realtime priority failure，而不是只看最终根因文字。
    """

    suite = load_history_impact_eval_suite(SUITE_PATH)
    runner = ScriptedHistoryImpactRunner(remove_conflict_warning=True)

    report = await evaluate_history_impact(suite, runner)

    assert report.conflict_guard_pass_rate == 0
    assert report.realtime_priority_failure_count == 1


def _diagnosis_result(
    case: HistoryImpactEvalCase,
    *,
    mode: HistoryImpactMode,
    trigger_override: HistoryTrigger | None,
    remove_conflict_warning: bool,
) -> DiagnosisRunResult:
    """构造包含真实领域模型、事件、报告和 staging 的单模式合成终态。

    函数先创建本次 TOOL Evidence/支持假设与实际 ToolEvent，再按模式加入 confirmed memory 和确定性
    matcher 解释，最后组装 ReAct、Auditor 与顶层结果。任何跨阶段不一致都由生产模型立即暴露。
    """

    mode_slug = mode.value
    run_id = f"run_{_digest(case.case_id, mode_slug)}"
    session_id = f"session_{_digest(case.case_id, mode_slug, 'session')}"
    current_root = case.allowed_root_causes[0]
    current_evidence = Evidence(
        evidence_id=f"ev_{_digest(case.case_id, mode_slug, 'current')}",
        source_type=EvidenceSourceType.TOOL,
        source_id=f"synthetic-current-{case.scenario_id}",
        content=f"本次只读 Observation 支持当前根因：{current_root}。",
        observed_at=NOW,
        reliability=0.97,
    )
    selected_tool = _selected_tool(case, mode)
    tool_event = _tool_event(case, run_id=run_id, tool_name=selected_tool)
    state = AgentState(
        run_id=run_id,
        session_id=session_id,
        user_query=case.user_query,
        hypotheses=[
            FaultHypothesis(
                hypothesis_id=f"hyp_{_digest(case.case_id, mode_slug)}",
                symptom=f"{case.components[0].value} 合成故障",
                candidate_root_cause=current_root,
                components=list(case.components),
                supporting_evidence=[current_evidence.evidence_id],
                status=HypothesisStatus.SUPPORTED,
                confidence=0.9,
            )
        ],
        evidence=[current_evidence],
        tool_events=[tool_event],
        react_step=1,
        observation_refs=[current_evidence.evidence_id],
        stop_reason="evidence_sufficient",
    )
    selection = get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=tuple(case.components),
            history_trigger=(
                HistoryTrigger.USER_REQUESTED
                if mode is HistoryImpactMode.MEMORY_ON
                else HistoryTrigger.NOT_REQUESTED
            ),
        )
    )
    state = state.model_copy(
        update={
            "intent": selection.intent.value,
            "active_capabilities": [item.value for item in selection.active_capabilities],
        }
    )

    recalled = (_history_match(case),) if mode is HistoryImpactMode.MEMORY_ON else ()
    explanations = explain_case_matches(
        recalled,
        state,
        current_components=tuple(case.components),
    )
    if remove_conflict_warning and case.expect_history_conflict and explanations:
        explanations = (
            explanations[0].model_copy(update={"pitfall_warnings": ["历史方案仍需人工复核。"]}),
        )

    report = DiagnosisReport(
        summary="合成诊断报告已通过结构化审计。",
        root_causes=[
            RootCauseConclusion(
                root_cause=current_root,
                confidence=0.9,
                evidence_refs=[current_evidence.evidence_id],
            )
        ],
        evidence_refs=[
            current_evidence.evidence_id,
            *(item.case_id for item in explanations),
        ],
        similar_cases=list(explanations),
    )
    audit = AuditResult(status=AuditStatus.ACCEPT)
    pending = _pending_memory(case, mode=mode, evidence_id=current_evidence.evidence_id)
    final_state = state.model_copy(
        update={
            "draft_report": report,
            "audit_result": audit,
            "memory_candidate": pending,
        }
    )
    react = ReactRunResult(
        contract_id=REACT_LOOP_CONTRACT_ID,
        state=state,
        capabilities=selection,
        events=_react_events(case, selected_tool=selected_tool),
    )
    report_result = ReportRunResult(
        contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
        state=final_state,
        outcome=ReportWorkflowOutcome.ACCEPTED,
        events=_report_events(case),
    )
    trigger = trigger_override or (
        HistoryTrigger.USER_REQUESTED
        if mode is HistoryImpactMode.MEMORY_ON
        else HistoryTrigger.NOT_REQUESTED
    )
    return DiagnosisRunResult(
        contract_id=DIAGNOSIS_WORKFLOW_CONTRACT_ID,
        history_trigger=trigger,
        memory_query=case.user_query if trigger is not HistoryTrigger.NOT_REQUESTED else None,
        recalled_memories=recalled,
        history_case_matches=explanations,
        react=react,
        report=report_result,
        memory_stage=MemoryStageResult(status=MemoryStageStatus.STAGED, memory=pending),
    )


def _selected_tool(case: HistoryImpactEvalCase, mode: HistoryImpactMode) -> ToolName:
    """按评测脚本选择实际工具，使第一例只在 memory-on 命中必要 Action。

    其余案例始终选择 fixture 的首个必要工具；action-guidance 的 off 组故意选择同组件但未标注的
    状态工具，形成一个可解释的意外 Action，而不是 capability 越界或不存在的工具。
    """

    if case.case_id == "history_impact_action_guidance" and mode is HistoryImpactMode.MEMORY_OFF:
        return ToolName.LTS_GET_TASK_STATUS
    return case.required_tool_names[0]


def _tool_event(
    case: HistoryImpactEvalCase,
    *,
    run_id: str,
    tool_name: ToolName,
) -> ToolEvent:
    """构造一个成功的合成只读 ToolEvent，证明 Action 已实际进入执行边界。

     请求使用带时区窗口、合成 scenario 和当前 trace；响应不携带额外 evidence，因为根因引用来自
    预置的本次 TOOL Observation。事件时间严格递增并通过生产模型校验。
    """

    request = McpToolRequest(
        resource_id=f"resource-{case.case_id}",
        time_range=TimeRange(start=NOW - timedelta(minutes=5), end=NOW),
        scenario_id=case.scenario_id,
        trace_id=run_id,
    )
    response = McpToolResponse(ok=True, data={"status": "synthetic"}, observed_at=NOW)
    return ToolEvent(
        event_id=f"evt_{_digest(case.case_id, run_id, tool_name.value)}",
        trace_id=run_id,
        tool_name=tool_name,
        request=request,
        response=response,
        attempt=1,
        retryable=False,
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=10),
    )


def _history_match(case: HistoryImpactEvalCase) -> CaseMemoryMatch:
    """构造一个 confirmed vector 命中，冲突案例使用 fixture 的 forbidden 旧根因。

    相似度只表示检索相关性；CaseMemory 不含 embedding。历史证据 ID 与本次 TOOL Evidence 分离，
    让 matcher 和评测器能够验证旧案例没有冒充当前观察。
    """

    root_cause = (
        case.forbidden_root_causes[0]
        if case.expect_history_conflict
        else case.allowed_root_causes[0]
    )
    memory = CaseMemory(
        memory_id=f"mem_{_digest(case.case_id, 'history')}",
        symptoms=[case.user_query],
        root_cause=root_cause,
        fault_path=["合成历史链路"],
        solution_steps=["仅在隔离环境人工复核历史方案。"],
        components=list(case.components),
        tags=["history_impact_eval"],
        evidence_refs=[f"ev_{_digest(case.case_id, 'history')}"],
        status=MemoryStatus.CONFIRMED,
        occurrence_count=2,
        created_at=NOW - timedelta(days=3),
        updated_at=NOW - timedelta(days=1),
    )
    return CaseMemoryMatch(
        memory=memory,
        similarity=0.91,
        retrieval_channels=[MemoryRetrievalChannel.VECTOR],
        direct_similarity=0.91,
    )


def _pending_memory(
    case: HistoryImpactEvalCase,
    *,
    mode: HistoryImpactMode,
    evidence_id: str,
) -> CaseMemory:
    """构造与 accepted 报告一致的新 pending memory staging 结果。

    评测不测试去重，因此每个 mode 使用独立 ID；状态保持 pending，体现 Auditor 接受不等于自动
    confirmed。证据只关联本次根因引用，不复制历史案例的旧 Evidence。
    """

    return CaseMemory(
        memory_id=f"mem_{_digest(case.case_id, mode.value, 'pending')}",
        symptoms=[case.user_query],
        root_cause=case.allowed_root_causes[0],
        components=list(case.components),
        evidence_refs=[evidence_id],
        status=MemoryStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


def _react_events(
    case: HistoryImpactEvalCase,
    *,
    selected_tool: ToolName,
) -> list[ReactPublicEvent]:
    """创建含能力选择、实际工具决策和公开停止原因的最小 ReAct 时间线。

    事件不包含 Thought；工具名只用于公开 Action 审计。实际覆盖仍从 ToolEvent 读取，因此删除或
    伪造 Planner 事件不会提高评测分数。
    """

    return [
        ReactPublicEvent(
            event_id=f"react_evt_{_digest(case.case_id, 'route')}",
            sequence=1,
            event_type=ReactEventType.CAPABILITIES_SELECTED,
            summary="合成能力选择完成。",
        ),
        ReactPublicEvent(
            event_id=f"react_evt_{_digest(case.case_id, 'action')}",
            sequence=2,
            event_type=ReactEventType.PLANNER_DECISION,
            summary="合成 Planner 选择一项只读检查。",
            tool_name=selected_tool,
        ),
        ReactPublicEvent(
            event_id=f"react_evt_{_digest(case.case_id, 'stop')}",
            sequence=3,
            event_type=ReactEventType.LOOP_STOPPED,
            summary="合成 Planner 已基于实时证据停止。",
            stop_reason="evidence_sufficient",
        ),
    ]


def _report_events(case: HistoryImpactEvalCase) -> list[ReportPublicEvent]:
    """创建草稿与独立审计接受两条公开报告事件。

    事件只记录结构化阶段和 accept 状态，不复制报告内容或 Auditor 推理；序号和稳定 ID 满足生产
    ReportRunResult 的终态门禁。
    """

    return [
        ReportPublicEvent(
            event_id=f"report_evt_{_digest(case.case_id, 'draft')}",
            sequence=1,
            event_type=ReportEventType.DRAFT_CREATED,
            summary="合成报告草稿已创建。",
            revision_number=0,
        ),
        ReportPublicEvent(
            event_id=f"report_evt_{_digest(case.case_id, 'accept')}",
            sequence=2,
            event_type=ReportEventType.AUDIT_COMPLETED,
            summary="合成独立审计已接受。",
            audit_status=AuditStatus.ACCEPT,
            revision_number=0,
        ),
    ]


def _digest(*parts: str) -> str:
    """把合成标识部件转换成满足事件 Schema 的稳定 16 位十六进制摘要。

    摘要只用于测试 ID 可重放，不用于密码学认证；分隔符避免简单拼接歧义。相同 case/mode 始终
    得到相同标识，使失败快照易于人工复核。
    """

    return sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
