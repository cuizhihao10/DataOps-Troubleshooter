"""用十六条 Golden Cases 验证顶层诊断、零工具补参、路径、冲突、记忆与 16/28 边界。

测试运行器从合成 Fixture 构造真实 ``ToolEvent``/``Evidence``，再通过生产 Pydantic 顶层结果契约
进入评测器。Planner、Auditor 和报告文本是确定性脚本，因此这些数字只证明数据流与评分规则可
重复，不代表真实 LLM 准确率；真实 MCP 协议和 LangGraph 控制流由各自集成测试独立覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from app.capabilities import CapabilitySelectionRequest, DiagnosisIntent, HistoryTrigger
from app.capabilities.registry import get_capability_registry
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.domain.models import (
    AgentState,
    AuditResult,
    AuditStatus,
    CaseMemory,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultChainStep,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
    RemediationStep,
    RetrievedPath,
    RiskLevel,
    RootCauseConclusion,
    SimilarCaseReference,
    ToolEvent,
)
from app.domain.scenarios import GoldenCaseCategory, GoldenCaseSpec
from app.evaluation.golden_diagnosis import (
    GOLDEN_DIAGNOSIS_EVAL_CONTRACT_ID,
    evaluate_golden_diagnosis,
)
from app.memory.models import (
    CaseMemoryMatch,
    MemoryRetrievalChannel,
    MemoryStageResult,
    MemoryStageStatus,
)
from app.orchestration.diagnosis_models import (
    DIAGNOSIS_WORKFLOW_CONTRACT_ID,
    DiagnosisRunResult,
)
from app.orchestration.models import (
    REACT_LOOP_CONTRACT_ID,
    ReactEventType,
    ReactPublicEvent,
    ReactRunResult,
)
from app.orchestration.report_models import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    ReportEventType,
    ReportPublicEvent,
    ReportRunResult,
    ReportWorkflowOutcome,
)

FIXTURE_DIRECTORY = Path("data/fixtures/scenarios")
GOLDEN_CASE_FILE = Path("data/fixtures/golden_cases.json")
NOW = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


class FixtureBackedGoldenRunner:
    """把 Golden 标注映射为确定性、强类型的完整诊断回归基线。

    该 runner 只用于验证评测管线：Action 响应来自已校验 Fixture，允许根因用于构造预期通过的
    报告。它不冒充 Planner 模型，也不绕过评测器读取最终分数；每条结果仍需通过所有生产模型。
    """

    def __init__(self, registry: FixtureRegistry) -> None:
        """保存只读 Fixture 注册表，不预先执行案例或缓存结果。

        注册表已经在加载时完成 scenario/schema 唯一性校验；构造器不访问网络、MCP 或数据库，
        因而测试结果只依赖版本控制内的合成数据。
        """

        self._registry = registry

    async def run(self, case: GoldenCaseSpec) -> DiagnosisRunResult:
        """按必要工具回放合成响应，并组装已审计的顶层诊断结果。

        每个必要工具只选择场景中第一个同名结果；当前 Golden 标注保证该映射唯一且能覆盖所需
        evidence source。找不到工具会显式失败，避免把 Fixture/标注漂移变成较低覆盖率。
        """

        scenario = self._registry.get(case.scenario_id)
        # 普通案例由实际工具前缀收敛组件；零工具补参案例只能使用 Scenario 的已校验组件元数据。
        components = (
            _components_from_tools(case)
            if case.required_tools
            else tuple(scenario.components)
        )
        run_id = f"run_{_digest(case.case_id)}"
        session_id = f"session_{_digest(case.case_id, 'session')}"
        tool_events: list[ToolEvent] = []
        evidence: list[Evidence] = []

        for index, required_tool in enumerate(case.required_tools, start=1):
            matches = [item for item in scenario.tool_results if item.tool_name is required_tool]
            if not matches:
                raise AssertionError(
                    f"Golden case {case.case_id} references missing tool {required_tool.value}"
                )
            fixture_result = matches[0]
            request = fixture_result.request.model_copy(update={"trace_id": run_id})
            started_at = NOW + timedelta(milliseconds=index * 20)
            tool_events.append(
                ToolEvent(
                    event_id=f"evt_{_digest(case.case_id, required_tool.value)}",
                    trace_id=run_id,
                    tool_name=required_tool,
                    request=request,
                    response=fixture_result.response,
                    attempt=1,
                    retryable=False,
                    started_at=started_at,
                    completed_at=started_at + timedelta(milliseconds=10),
                )
            )
            # Fixture 的 ToolEvidencePayload 是 MCP 公开证据；测试用稳定 ID 投影为领域 Evidence。
            for payload in fixture_result.response.evidence:
                evidence.append(
                    Evidence(
                        evidence_id=f"ev_{_digest(case.case_id, payload.source_id)}",
                        source_type=EvidenceSourceType.TOOL,
                        source_id=payload.source_id,
                        content=payload.content,
                        observed_at=fixture_result.response.observed_at,
                        reliability=0.95,
                        metadata=payload.metadata,
                    )
                )

        return _build_diagnosis_result(
            case,
            run_id=run_id,
            session_id=session_id,
            tool_events=tool_events,
            evidence=evidence,
            components=components,
        )


@pytest.mark.asyncio
async def test_sixteen_golden_cases_produce_versioned_measured_diagnosis_baseline() -> None:
    """验证十六条案例命中诊断、补参、路径、冲突与历史安全契约，并保持 16/28 未完成标记。

    确定性基线预期意图、必要 Action、允许根因、关键来源、停止原因、引用、风险和安全降级全部
    命中；三条故意失败响应使尝试成功率低于一，新增冲突案例的三个调用则全部成功。覆盖标记必须
    保持 false，防止 16 条通过被宣传为 28 条验收完成；零工具案例必须在任何 ToolEvent 前停止，
    三条记忆案例仍需保持实时根因优先，资源耗尽案例继续使用独立 Fixture 和反证来源。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    runner = FixtureBackedGoldenRunner(FixtureRegistry.from_directory(FIXTURE_DIRECTORY))

    report = await evaluate_golden_diagnosis(cases, runner)

    assert report.contract_id == GOLDEN_DIAGNOSIS_EVAL_CONTRACT_ID
    assert report.metric_kind == "measured"
    assert report.case_count == 16
    assert report.target_case_count == 28
    assert report.case_coverage_rate == pytest.approx(16 / 28)
    assert report.target_coverage_complete is False
    assert report.category_case_counts == {
        GoldenCaseCategory.SINGLE_COMPONENT: 4,
        GoldenCaseCategory.CROSS_COMPONENT: 4,
        GoldenCaseCategory.AMBIGUOUS_OR_INSUFFICIENT: 2,
        GoldenCaseCategory.TOOL_ANOMALY_OR_CONFLICT: 3,
        GoldenCaseCategory.MEMORY_RECALL: 3,
    }
    assert report.intent_accuracy == 1
    assert report.root_cause_top1_hit_rate == 1
    assert report.necessary_action_coverage == 1
    assert report.evidence_source_coverage == 1
    assert report.fault_path_completeness == 1
    assert report.stop_reason_hit_rate == 1
    assert report.citation_completeness == 1
    assert report.unsupported_critical_claim_rate == 0
    assert report.duplicate_action_rate == 0
    assert report.tool_attempt_success_rate == pytest.approx(42 / 45)
    assert report.risk_level_hit_rate == 1
    assert report.safe_degradation_rate == 1
    assert report.evidence_conflict_safe_resolution_rate == 1
    assert report.forbidden_conflict_root_hit_count == 0
    assert report.history_trigger_hit_rate == 1
    assert report.history_recall_coverage == 1
    assert report.confirmed_only_recall_rate == 1
    assert report.history_projection_pass_rate == 1
    assert report.realtime_priority_pass_rate == 1
    assert report.forbidden_memory_hit_count == 0
    assert report.accepted_report_rate == 1
    lts_bds_result = next(
        result
        for result in report.cases
        if result.case_id == "golden_cross_lts_blocked_by_bds_partition"
    )
    assert lts_bds_result.executed_tools == [
        "lts.get_task_status",
        "lts.get_dependency_topology",
        "bds.get_task_status",
        "bds.get_task_log",
        "bds.get_table_info",
    ]
    assert lts_bds_result.matched_fault_path_labels == [
        "lts_task_depends_on_bds_task",
        "bds_task_consumes_delayed_dataset",
    ]
    bds_flashsync_result = next(
        result
        for result in report.cases
        if result.case_id == "golden_cross_bds_blocked_by_flashsync_conflict"
    )
    assert bds_flashsync_result.executed_tools == [
        "bds.get_task_status",
        "bds.get_task_log",
        "bds.get_table_info",
        "flashsync.get_sync_delay",
        "flashsync.get_sync_log",
        "flashsync.check_consistency",
    ]
    assert bds_flashsync_result.matched_fault_path_labels == [
        "bds_task_depends_on_flashsync_task",
        "flashsync_task_produces_bds_dataset",
        "flashsync_backlog_conflict_solution_chain",
    ]
    resource_result = next(
        result
        for result in report.cases
        if result.case_id == "golden_cross_lts_blocked_by_bds_resource_exhaustion"
    )
    assert resource_result.executed_tools == [
        "lts.get_task_status",
        "lts.get_task_log",
        "lts.get_dependency_topology",
        "bds.get_task_status",
        "bds.get_task_log",
        "bds.get_table_info",
    ]
    assert resource_result.matched_fault_path_labels == [
        "lts_component_depends_on_bds_component"
    ]
    missing_context_result = next(
        result
        for result in report.cases
        if result.case_id == "golden_ambiguous_bds_missing_resource_context"
    )
    assert missing_context_result.executed_tools == []
    assert missing_context_result.logical_action_count == 0
    assert missing_context_result.actual_stop_reason == "missing_resource_id"
    assert missing_context_result.safe_degradation_hit is True


@pytest.mark.asyncio
async def test_evidence_conflict_rejects_cited_root_and_hidden_uncertainty() -> None:
    """确认有效引用不能掩盖证据冲突案例中的武断根因和不确定性缺失。

    负向结果保留三个成功 ToolEvent/Evidence，却注入 Golden 明确禁止的单侧根因，并清空
    uncertainties。根因引用使用真实 Evidence ID，所以结构引用完整率仍为一；专用冲突安全指标
    必须失败并分别暴露禁止根因命中和未公开不确定性，证明它不是 citation 指标的重复包装。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    target = next(
        case
        for case in cases
        if case.case_id == "golden_bds_conflicting_partition_evidence"
    )
    baseline = await FixtureBackedGoldenRunner(
        FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    ).run(target)
    report = baseline.report.state.draft_report
    assert report is not None
    evidence_id = baseline.react.state.evidence[0].evidence_id
    unsafe_report = report.model_copy(
        update={
            "root_causes": [
                RootCauseConclusion(
                    root_cause="BDS 上游分区缺失",
                    confidence=0.9,
                    evidence_refs=[evidence_id],
                )
            ],
            "uncertainties": [],
        }
    )
    unsafe_state = baseline.report.state.model_copy(update={"draft_report": unsafe_report})
    diagnosis = baseline.model_copy(
        update={"report": baseline.report.model_copy(update={"state": unsafe_state})}
    )

    result = (await evaluate_golden_diagnosis([target], _SingleResultRunner(diagnosis))).cases[0]

    assert result.citation_completeness == 1
    assert result.missing_conflicting_evidence_sources == []
    assert result.forbidden_conflict_root_hits == ["BDS 上游分区缺失"]
    assert result.conflict_uncertainty_disclosed is False
    assert result.evidence_conflict_safe_resolution is False


@pytest.mark.asyncio
async def test_evidence_conflict_requires_every_annotated_source_to_be_observed() -> None:
    """确认报告即使保持空根因并公开 uncertainty，也不能掩盖冲突来源缺失。

    测试从通过基线删除表元数据 Evidence，但保留其成功 ToolEvent 和安全报告。评分器必须把稳定
    source ID 列入 missing，并让冲突安全处置失败；这证明指标先验证事实输入完整性，再评价报告是否
    克制，而不是看到空根因就自动给满分。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    target = next(
        case
        for case in cases
        if case.case_id == "golden_bds_conflicting_partition_evidence"
    )
    baseline = await FixtureBackedGoldenRunner(
        FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    ).run(target)
    retained_evidence = [
        evidence
        for evidence in baseline.react.state.evidence
        if evidence.source_id != "bds_conflict_table_inventory"
    ]
    retained_ids = [evidence.evidence_id for evidence in retained_evidence]
    incomplete_state = baseline.react.state.model_copy(
        update={"evidence": retained_evidence, "observation_refs": retained_ids}
    )
    diagnosis = baseline.model_copy(
        update={"react": baseline.react.model_copy(update={"state": incomplete_state})}
    )

    result = (await evaluate_golden_diagnosis([target], _SingleResultRunner(diagnosis))).cases[0]

    assert result.conflict_uncertainty_disclosed is True
    assert result.missing_conflicting_evidence_sources == ["bds_conflict_table_inventory"]
    assert result.evidence_conflict_safe_resolution is False


@pytest.mark.asyncio
async def test_golden_diagnosis_evaluator_exposes_missing_action_and_unsafe_root() -> None:
    """确认评分器不会被 accept 标签掩盖缺失 Action 或证据不足案例的猜测根因。

    runner 先生成强类型通过结果，再只对空日志案例注入一个无效引用根因并删除唯一 ToolEvent；
    顶层结构仍合法，但评测必须把 Action 覆盖、安全降级和引用完整性分别降为零。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    target = next(case for case in cases if case.case_id == "golden_lts_empty_result")
    baseline = await FixtureBackedGoldenRunner(
        FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    ).run(target)
    report = baseline.report.state.draft_report
    assert report is not None
    unsafe_report = report.model_copy(
        update={
            "root_causes": [
                RootCauseConclusion(
                    root_cause="无实时依据的猜测根因",
                    confidence=0.9,
                    evidence_refs=["ev_missing"],
                )
            ]
        }
    )
    unsafe_react_state = baseline.react.state.model_copy(update={"tool_events": []})
    unsafe_report_state = baseline.report.state.model_copy(update={"draft_report": unsafe_report})
    unsafe = baseline.model_copy(
        update={
            "react": baseline.react.model_copy(update={"state": unsafe_react_state}),
            "report": baseline.report.model_copy(update={"state": unsafe_report_state}),
        }
    )

    result = (await evaluate_golden_diagnosis([target], _SingleResultRunner(unsafe))).cases[0]

    assert result.necessary_action_coverage == 0
    assert result.missing_required_tools == ["lts.get_task_log"]
    assert result.safe_degradation_hit is False
    assert result.citation_completeness == 0
    assert result.unsupported_critical_claim_count == 1


@pytest.mark.asyncio
async def test_golden_diagnosis_requires_retrieved_path_to_be_used_by_final_report() -> None:
    """确认仅检索到正确图路径但最终报告未引用时，链路完整率仍为零。

    主案例基线含两条完整 RetrievedPath；测试删除最终 fault_chain，但保留检索状态和根因引用。评分器
    必须把两个路径标签都列为 missing，证明指标衡量“检索并使用”而不是仅检查候选池。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    target = next(case for case in cases if case.case_id == "golden_cross_chain_pk_conflict")
    baseline = await FixtureBackedGoldenRunner(
        FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    ).run(target)
    report = baseline.report.state.draft_report
    assert report is not None
    unreported = report.model_copy(update={"fault_chain": []})
    unreported_state = baseline.report.state.model_copy(update={"draft_report": unreported})
    diagnosis = baseline.model_copy(
        update={"report": baseline.report.model_copy(update={"state": unreported_state})}
    )

    result = (await evaluate_golden_diagnosis([target], _SingleResultRunner(diagnosis))).cases[0]

    assert result.fault_path_completeness == 0
    assert result.matched_fault_path_labels == []
    assert result.missing_fault_path_labels == [
        "component_dependency_chain",
        "sync_backlog_causal_chain",
    ]


@pytest.mark.asyncio
async def test_memory_golden_requires_recalled_case_projection_in_final_report() -> None:
    """确认 raw confirmed memory 已召回但最终报告未展示时，历史投影门禁失败。

    测试保留 DiagnosisRunResult 的 recalled_memories/history_case_matches，只清空报告
    similar_cases；召回覆盖仍为一，但 projection 必须为 false，防止后端命中历史而报告不可解释。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    target = next(case for case in cases if case.case_id == "golden_memory_lts_action_guidance")
    baseline = await FixtureBackedGoldenRunner(
        FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    ).run(target)
    report = baseline.report.state.draft_report
    assert report is not None
    hidden_history = report.model_copy(update={"similar_cases": []})
    hidden_state = baseline.report.state.model_copy(update={"draft_report": hidden_history})
    diagnosis = baseline.model_copy(
        update={"report": baseline.report.model_copy(update={"state": hidden_state})}
    )

    result = (await evaluate_golden_diagnosis([target], _SingleResultRunner(diagnosis))).cases[0]

    assert result.history_recall_coverage == 1
    assert result.history_projection_complete is False


@pytest.mark.asyncio
async def test_memory_golden_rejects_historical_root_without_realtime_tool_support() -> None:
    """确认有效 memory ID 不能单独支撑与本次 Observation 冲突的最终根因。

    负向结果把 BDS 当前缺分区根因替换成旧数据倾斜根因，并只引用 confirmed memory ID；结构引用
    完整率仍可为一，但实时优先指标必须失败，体现历史参考与当前事实的职责分离。
    """

    cases = load_golden_cases(GOLDEN_CASE_FILE)
    target = next(case for case in cases if case.case_id == "golden_memory_bds_conflict_guard")
    baseline = await FixtureBackedGoldenRunner(
        FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    ).run(target)
    report = baseline.report.state.draft_report
    assert report is not None
    memory_id = baseline.recalled_memories[0].memory.memory_id
    unsafe_report = report.model_copy(
        update={
            "root_causes": [
                RootCauseConclusion(
                    root_cause="BDS 数据倾斜",
                    confidence=0.9,
                    evidence_refs=[memory_id],
                )
            ]
        }
    )
    unsafe_state = baseline.report.state.model_copy(update={"draft_report": unsafe_report})
    diagnosis = baseline.model_copy(
        update={"report": baseline.report.model_copy(update={"state": unsafe_state})}
    )

    result = (await evaluate_golden_diagnosis([target], _SingleResultRunner(diagnosis))).cases[0]

    assert result.citation_completeness == 1
    assert result.realtime_priority_preserved is False


class _SingleResultRunner:
    """在负向单测中返回一个预先组装的强类型诊断结果。

    类只隔离异步 runner 协议，不修改输入案例或结果；如果被意外用于不同 scenario，会显式失败，
    防止负向测试结果泄漏到其他案例。
    """

    def __init__(self, result: DiagnosisRunResult) -> None:
        """保存不可变测试结果引用，不执行任何 I/O 或模型校验之外的转换。

        ``DiagnosisRunResult`` 已在调用前构造并通过 Pydantic；构造器没有失败降级或默认返回值。
        """

        self._result = result

    async def run(self, case: GoldenCaseSpec) -> DiagnosisRunResult:
        """校验 scenario 一致后返回预置结果，满足评测器异步协议。

        不匹配时抛出 AssertionError，说明测试编排错误；正常返回不复制对象，因为模型在评分期间
        只读，且本 runner 仅允许调用一次。
        """

        actual_scenario = self._result.react.state.tool_events
        if actual_scenario and actual_scenario[0].request.scenario_id != case.scenario_id:
            raise AssertionError("single-result runner received a different Golden scenario")
        return self._result


def _build_diagnosis_result(
    case: GoldenCaseSpec,
    *,
    run_id: str,
    session_id: str,
    tool_events: list[ToolEvent],
    evidence: list[Evidence],
    components: tuple[Component, ...],
) -> DiagnosisRunResult:
    """从 Fixture 回放产物构造满足生产跨阶段不变量的诊断终态。

    有允许根因时创建受实时证据支持的假设、根因与 pending memory；无允许根因时输出不确定性并
    安全跳过记忆。``components`` 通常来自 required tool，零工具补参案例则来自已校验 Scenario，确保
    能力选择仍由生产 registry 完成，而不是为通过测试手写 capability 名称。
    """

    intent = DiagnosisIntent(case.expected_intent)
    history_trigger = (
        HistoryTrigger.USER_REQUESTED
        if case.history_expectation is not None
        else HistoryTrigger.NOT_REQUESTED
    )
    selection = get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=intent,
            components=components,
            history_trigger=history_trigger,
        )
    )
    evidence_ids = [item.evidence_id for item in evidence]
    retrieved_paths = _build_retrieved_paths(case)
    recalled_memories, history_explanations = _build_history_context(case, components)
    root_cause = case.allowed_root_causes[0] if case.allowed_root_causes else None
    hypotheses = []
    if root_cause:
        hypotheses.append(
            FaultHypothesis(
                hypothesis_id=f"hyp_{_digest(case.case_id)}",
                symptom="版本化合成 Golden Case 故障现象",
                candidate_root_cause=root_cause,
                components=list(components),
                supporting_evidence=evidence_ids,
                status=HypothesisStatus.SUPPORTED,
                confidence=0.9,
            )
        )

    state = AgentState(
        run_id=run_id,
        session_id=session_id,
        user_query=case.user_query,
        intent=selection.intent.value,
        active_capabilities=[item.value for item in selection.active_capabilities],
        hypotheses=hypotheses,
        evidence=evidence,
        tool_events=tool_events,
        retrieved_paths=retrieved_paths,
        react_step=len(tool_events),
        observation_refs=evidence_ids,
        stop_reason=case.expected_stop_reasons[0],
    )
    report = _build_report(
        case,
        root_cause=root_cause,
        evidence_ids=evidence_ids,
        retrieved_paths=retrieved_paths,
        similar_cases=history_explanations,
    )
    memory = _build_pending_memory(case, components, evidence_ids) if root_cause else None
    final_state = state.model_copy(
        update={
            "draft_report": report,
            "audit_result": AuditResult(status=AuditStatus.ACCEPT),
            "memory_candidate": memory,
        }
    )
    react = ReactRunResult(
        contract_id=REACT_LOOP_CONTRACT_ID,
        state=state,
        capabilities=selection,
        events=_react_events(case, stop_reason=state.stop_reason or "missing"),
    )
    report_result = ReportRunResult(
        contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
        state=final_state,
        outcome=ReportWorkflowOutcome.ACCEPTED,
        events=_report_events(case),
    )
    memory_stage = (
        MemoryStageResult(status=MemoryStageStatus.STAGED, memory=memory)
        if memory is not None
        else MemoryStageResult(status=MemoryStageStatus.SKIPPED_NO_ROOT_CAUSE)
    )
    return DiagnosisRunResult(
        contract_id=DIAGNOSIS_WORKFLOW_CONTRACT_ID,
        history_trigger=history_trigger,
        memory_query=case.user_query if case.history_expectation is not None else None,
        recalled_memories=recalled_memories,
        history_case_matches=history_explanations,
        react=react,
        report=report_result,
        memory_stage=memory_stage,
    )


def _build_report(
    case: GoldenCaseSpec,
    *,
    root_cause: str | None,
    evidence_ids: list[str],
    retrieved_paths: list[RetrievedPath],
    similar_cases: tuple[SimilarCaseReference, ...],
) -> DiagnosisReport:
    """生成引用完整且风险与 Golden 标注一致的确定性报告。

    根因案例引用全部本次 TOOL Evidence 并形成一段可审计链路；异常/空结果案例不猜根因，只给
    出低风险人工核验建议和明确不确定性。所有建议仅是说明，不执行生产写操作。
    """

    remediation = RemediationStep(
        order=1,
        action="由人工在隔离演示环境复核证据后再决定处置。",
        risk_level=case.expected_risk_level,
        evidence_refs=evidence_ids if case.expected_risk_level is RiskLevel.HIGH else [],
        prerequisites=["确认当前仅使用合成或 Mock 数据"],
        rollback="本步骤不执行写操作，无需生产回滚。",
        verification="复核报告引用和工具事件与 Golden 标注一致。",
    )
    if root_cause:
        return DiagnosisReport(
            summary="确定性 Golden 基线已形成有证据的候选根因。",
            fault_chain=[
                FaultChainStep(
                    description=f"合成 GraphRAG 路径 {path.path_id} 支持当前故障传播结论。",
                    evidence_refs=[path.path_id],
                )
                for path in retrieved_paths
            ],
            root_causes=[
                RootCauseConclusion(
                    root_cause=root_cause,
                    confidence=0.9,
                    evidence_refs=evidence_ids,
                )
            ],
            evidence_refs=[
                *evidence_ids,
                *(path.path_id for path in retrieved_paths),
                *(similar.case_id for similar in similar_cases),
            ],
            remediation_steps=[remediation],
            similar_cases=list(similar_cases),
        )
    return DiagnosisReport(
        summary="当前工具结果不足以确认根因，保持安全降级。",
        remediation_steps=[remediation],
        uncertainties=["缺少可支持根因的实时成功 Observation，需补充权限或稍后重试。"],
    )


def _build_history_context(
    case: GoldenCaseSpec,
    components: tuple[Component, ...],
) -> tuple[tuple[CaseMemoryMatch, ...], tuple[SimilarCaseReference, ...]]:
    """把 Golden memory 标注投影为 confirmed raw match 与可解释报告引用。

    非记忆案例返回两个空 tuple；记忆案例按标注顺序构造 confirmed CaseMemoryMatch，并让解释保留
    同一 ID/相似度。冲突案例明确写入根因差异和禁止直接复用提示，供实时优先门禁审阅。
    """

    if case.history_expectation is None:
        return (), ()

    matches: list[CaseMemoryMatch] = []
    explanations: list[SimilarCaseReference] = []
    for index, expectation in enumerate(case.history_expectation.required_memories, start=1):
        memory = CaseMemory(
            memory_id=expectation.memory_id,
            symptoms=[f"历史合成症状：{case.user_query}"],
            root_cause=expectation.historical_root_cause,
            fault_path=["历史合成故障路径"],
            solution_steps=["仅在隔离环境人工复核历史方案。"],
            components=list(components),
            tags=["golden_memory", case.scenario_id],
            evidence_refs=[f"ev_{_digest(case.case_id, expectation.memory_id)}"],
            status=MemoryStatus.CONFIRMED,
            occurrence_count=2,
            created_at=NOW - timedelta(days=index + 2),
            updated_at=NOW - timedelta(days=index),
        )
        matches.append(
            CaseMemoryMatch(
                memory=memory,
                similarity=expectation.similarity,
                retrieval_channels=[MemoryRetrievalChannel.VECTOR],
                direct_similarity=expectation.similarity,
            )
        )
        differences = (
            [
                f"历史根因 {expectation.historical_root_cause} 与本次允许根因不同，"
                "必须服从实时 Observation。"
            ]
            if expectation.expect_root_conflict
            else ["历史案例时间与本次运行不同，仍需核对实时 Observation。"]
        )
        pitfalls = (
            ["禁止直接复用历史根因或处置方案，先验证本次实时证据。"]
            if expectation.expect_root_conflict
            else ["历史方案只作参考，执行前仍需检查当前环境。"]
        )
        explanations.append(
            SimilarCaseReference(
                case_id=expectation.memory_id,
                similarity=expectation.similarity,
                confirmed=True,
                common_points=["组件和合成症状具有可复核共同点。"],
                differences=differences,
                reference_actions=["优先执行本次 Golden 标注的只读工具检查。"],
                pitfall_warnings=pitfalls,
                evidence_refs=[expectation.memory_id],
            )
        )
    return tuple(matches), tuple(explanations)


def _build_retrieved_paths(case: GoldenCaseSpec) -> list[RetrievedPath]:
    """把 Golden v2 必要路径标注投影为确定性 RetrievedPath 运行结果。

    该投影只用于评分管线基线，不伪装成 PostgreSQL 检索；真实 GraphRAG 路径由独立数据库测试验证。
    稳定 path_id 与有序节点/关系完整保留，使最终报告必须引用同一对象才能获得链路分数。
    """

    return [
        RetrievedPath(
            path_id=f"path_{_digest(case.case_id, requirement.path_label)}",
            node_ids=requirement.required_node_ids,
            relation_types=list(requirement.required_relation_types),
            score=0.9,
            source_ids=["synthetic_cross_chain_knowledge_v1"],
        )
        for requirement in case.required_fault_paths
    ]


def _build_pending_memory(
    case: GoldenCaseSpec,
    components: tuple[Component, ...],
    evidence_ids: list[str],
) -> CaseMemory:
    """为有根因且审计接受的脚本报告生成 pending 记忆候选。

    候选只引用本次 Evidence，保持默认 pending，不自动确认；时间固定使测试可重复。调用方保证
    allowed root 和 Evidence 非空，否则领域模型会显式拒绝无依据候选。
    """

    return CaseMemory(
        memory_id=f"mem_{_digest(case.case_id)}",
        symptoms=[case.user_query],
        root_cause=case.allowed_root_causes[0],
        fault_path=["合成 Golden 诊断路径"],
        solution_steps=["人工复核后处置"],
        components=list(components),
        tags=["golden", case.scenario_id],
        evidence_refs=evidence_ids,
        status=MemoryStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


def _components_from_tools(case: GoldenCaseSpec) -> tuple[Component, ...]:
    """按必要工具首次出现顺序推导案例涉及的受支持组件。

    工具名是受控枚举并以组件前缀命名，因此转换不会解析用户自由文本；重复组件去重但保持顺序。
    空工具案例会显式失败，因为当前能力 registry 至少需要一个组件。
    """

    components = tuple(
        dict.fromkeys(Component(tool.value.split(".", 1)[0]) for tool in case.required_tools)
    )
    if not components:
        raise ValueError("Golden diagnosis fixture runner requires at least one required tool")
    return components


def _react_events(case: GoldenCaseSpec, *, stop_reason: str) -> list[ReactPublicEvent]:
    """构造最小公开 ReAct 起止时间线，不包含 Planner Thought 或工具响应正文。

    两个稳定事件满足生产终态模型；工具细节已经由 ``ToolEvent`` 保存，评测本身不依赖此脚本
    时间线计分。停止事件显式携带 Golden 允许原因。
    """

    return [
        ReactPublicEvent(
            event_id=f"react_evt_{_digest(case.case_id, 'selected')}",
            sequence=1,
            event_type=ReactEventType.CAPABILITIES_SELECTED,
            summary="已选择确定性 Golden 能力边界。",
        ),
        ReactPublicEvent(
            event_id=f"react_evt_{_digest(case.case_id, 'stopped')}",
            sequence=2,
            event_type=ReactEventType.LOOP_STOPPED,
            summary="Golden 脚本已完成必要只读 Action。",
            stop_reason=stop_reason,
        ),
    ]


def _report_events(case: GoldenCaseSpec) -> list[ReportPublicEvent]:
    """构造报告草稿与独立 Auditor 接受两个公开事件。

    事件不携带报告全文或隐藏推理，只证明顶层结果经历了报告终态；确定性脚本的 accept 不能被
    解释为真实 LLM Auditor 准确率。
    """

    return [
        ReportPublicEvent(
            event_id=f"report_evt_{_digest(case.case_id, 'draft')}",
            sequence=1,
            event_type=ReportEventType.DRAFT_CREATED,
            summary="已生成结构化 Golden 报告草稿。",
            revision_number=0,
        ),
        ReportPublicEvent(
            event_id=f"report_evt_{_digest(case.case_id, 'audit')}",
            sequence=2,
            event_type=ReportEventType.AUDIT_COMPLETED,
            summary="确定性 Auditor 脚本接受报告。",
            audit_status=AuditStatus.ACCEPT,
            revision_number=0,
        ),
    ]


def _digest(*parts: str) -> str:
    """把合成标识片段转换为事件模型要求的 16 位稳定十六进制后缀。

    SHA-256 这里只用于可重复 ID，不承担安全认证；分隔符防止不同片段拼接产生歧义。函数不读取
    凭据、时间或随机源，因此跨机器运行会得到相同结果。
    """

    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
