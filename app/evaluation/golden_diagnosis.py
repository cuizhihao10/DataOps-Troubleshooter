"""评估版本化 Golden Case 对完整诊断结果的客观契约命中情况。

本模块不调用模型、MCP 或数据库，而是消费 ``DiagnosisRunResult`` 这一顶层强类型结果；运行器可
替换为确定性测试替身或真实演示配置。评测只读取公开 Action、Observation、停止原因和已审计
报告，不读取 Prompt、模型原始响应或 Thought。报告同时保存总覆盖率与五类案例配额，不能把当前
子集实测写成产品 28 条目标已经达成。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import HistoryTrigger
from app.domain.models import EvidenceSourceType, MemoryStatus, RetrievedPath, RiskLevel
from app.domain.scenarios import (
    GoldenCaseCategory,
    GoldenCaseSpec,
    GoldenFaultPathRequirement,
)
from app.orchestration.diagnosis_models import DiagnosisRunResult
from app.orchestration.report_models import ReportWorkflowOutcome

GOLDEN_DIAGNOSIS_EVAL_CONTRACT_ID = "golden-diagnosis-eval:v10"
GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT = 28
GOLDEN_DIAGNOSIS_CATEGORY_TARGETS: dict[GoldenCaseCategory, int] = {
    GoldenCaseCategory.SINGLE_COMPONENT: 8,
    GoldenCaseCategory.CROSS_COMPONENT: 10,
    GoldenCaseCategory.AMBIGUOUS_OR_INSUFFICIENT: 4,
    GoldenCaseCategory.TOOL_ANOMALY_OR_CONFLICT: 3,
    GoldenCaseCategory.MEMORY_RECALL: 3,
}


class GoldenDiagnosisRunner(Protocol):
    """声明逐条运行 Golden Case 所需的最小异步诊断接口。

    评测器依赖协议而非具体模型供应商，使同一套评分逻辑既能验证确定性回归基线，也能验证真实
    LLM 配置。实现必须返回完整 ``DiagnosisRunResult``；异常应向上传播，不能伪装成零分案例。
    """

    async def run(self, case: GoldenCaseSpec) -> DiagnosisRunResult:
        """运行单条合成案例并返回已经完成 ReAct、Auditor 与记忆收尾的结果。

        ``case`` 提供输入和允许答案边界；实现不得读取这些允许答案后直接改写生产报告。I/O、超时
        和供应商错误保持显式传播，由调用评测命令的测试或 CLI 决定是否中止整套运行。
        """

        ...


class GoldenDiagnosisCaseResult(BaseModel):
    """保存单条案例的命中明细、分母和安全边界判断。

    集合字段保留实际与缺失项，便于失败时直接定位；比例均在零到一之间。没有允许根因的案例将
    ``root_cause_top1_hit`` 设为 ``None``，并改由 ``safe_degradation_hit`` 衡量是否克制输出。证据
    冲突案例另外保存来源精确分区、禁止根因命中和 uncertainty 义务，不能被普通引用完整率替代。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    scenario_id: str
    case_category: GoldenCaseCategory
    intent_hit: bool
    executed_tools: list[str]
    missing_required_tools: list[str]
    necessary_action_coverage: float = Field(ge=0, le=1)
    duplicate_action_count: int = Field(ge=0)
    logical_action_count: int = Field(ge=0)
    duplicate_action_rate: float = Field(ge=0, le=1)
    root_cause_top1_hit: bool | None
    actual_top1_root_cause: str | None = None
    observed_evidence_sources: list[str]
    missing_evidence_sources: list[str]
    evidence_source_coverage: float = Field(ge=0, le=1)
    required_fault_path_labels: list[str]
    matched_fault_path_labels: list[str]
    missing_fault_path_labels: list[str]
    matched_fault_path_ids: list[str]
    fault_path_completeness: float | None = Field(default=None, ge=0, le=1)
    stop_reason_hit: bool
    actual_stop_reason: str
    citation_completeness: float = Field(ge=0, le=1)
    unsupported_critical_claim_count: int = Field(ge=0)
    critical_claim_count: int = Field(ge=0)
    expected_risk_level: RiskLevel
    actual_risk_level: RiskLevel
    risk_level_hit: bool
    safe_degradation_hit: bool | None
    required_conflicting_evidence_sources: list[str]
    observed_conflicting_evidence_sources: list[str]
    missing_conflicting_evidence_sources: list[str]
    forbidden_conflict_root_hits: list[str]
    conflict_uncertainty_disclosed: bool | None = None
    evidence_conflict_safe_resolution: bool | None = None
    history_trigger_hit: bool | None = None
    required_memory_ids: list[str]
    recalled_memory_ids: list[str]
    missing_required_memory_ids: list[str]
    forbidden_memory_hits: list[str]
    history_recall_coverage: float | None = Field(default=None, ge=0, le=1)
    confirmed_only_recall: bool | None = None
    history_projection_complete: bool | None = None
    realtime_priority_preserved: bool | None = None
    tool_attempt_success_rate: float = Field(ge=0, le=1)
    report_accepted: bool

    @model_validator(mode="after")
    def validate_fault_path_partition(self) -> GoldenDiagnosisCaseResult:
        """校验路径、历史与冲突字段的精确分区和适用性。

        分区约束阻止同一路径同时成功和失败，或从明细中消失；无路径要求时 completeness 必须为
        ``None``，有要求时必须是数值。历史字段只用于 memory 类别；冲突来源也必须被 observed/missing
        完整划分，非冲突案例不能携带残留明细，防止聚合分母被可选字段静默污染。
        """

        required = self.required_fault_path_labels
        matched = self.matched_fault_path_labels
        missing = self.missing_fault_path_labels
        if len(required) != len(set(required)):
            raise ValueError("Golden case result required path labels must be unique")
        if set(matched) & set(missing) or set(matched) | set(missing) != set(required):
            raise ValueError("Golden case result path labels must form an exact partition")
        if bool(required) != (self.fault_path_completeness is not None):
            raise ValueError("Golden case result path applicability is inconsistent")
        if self.matched_fault_path_ids and not matched:
            raise ValueError("Golden case result path IDs require a matched requirement")
        is_memory_case = self.case_category is GoldenCaseCategory.MEMORY_RECALL
        optional_history_fields = (
            self.history_trigger_hit,
            self.history_recall_coverage,
            self.confirmed_only_recall,
            self.history_projection_complete,
            self.realtime_priority_preserved,
        )
        if is_memory_case != all(value is not None for value in optional_history_fields):
            raise ValueError("Golden case result history applicability is inconsistent")
        if not is_memory_case and any(
            (
                self.required_memory_ids,
                self.recalled_memory_ids,
                self.missing_required_memory_ids,
                self.forbidden_memory_hits,
            )
        ):
            raise ValueError("non-memory Golden case result cannot contain history identities")
        conflict_applicable = bool(self.required_conflicting_evidence_sources)
        optional_conflict_fields = (
            self.conflict_uncertainty_disclosed,
            self.evidence_conflict_safe_resolution,
        )
        if conflict_applicable != all(value is not None for value in optional_conflict_fields):
            raise ValueError("Golden case result conflict applicability is inconsistent")
        if conflict_applicable:
            if self.case_category is not GoldenCaseCategory.TOOL_ANOMALY_OR_CONFLICT:
                raise ValueError("Golden conflict result requires tool anomaly/conflict category")
            for field_name in (
                "required_conflicting_evidence_sources",
                "observed_conflicting_evidence_sources",
                "missing_conflicting_evidence_sources",
            ):
                values = getattr(self, field_name)
                if len(values) != len(set(values)):
                    raise ValueError(f"Golden conflict result {field_name} must be unique")
            required_conflict = set(self.required_conflicting_evidence_sources)
            observed_conflict = set(self.observed_conflicting_evidence_sources)
            missing_conflict = set(self.missing_conflicting_evidence_sources)
            if observed_conflict & missing_conflict:
                raise ValueError("Golden conflict evidence cannot be observed and missing")
            if observed_conflict | missing_conflict != required_conflict:
                raise ValueError("Golden conflict evidence must form an exact partition")
        elif any(
            (
                self.observed_conflicting_evidence_sources,
                self.missing_conflicting_evidence_sources,
                self.forbidden_conflict_root_hits,
            )
        ):
            raise ValueError("non-conflict Golden case result cannot contain conflict details")
        return self


class GoldenDiagnosisEvalReport(BaseModel):
    """汇总当前 Golden 子集的宏观实测指标和 28 条目标集覆盖资格。

    ``target_coverage_complete`` 只由案例数量决定，不由指标高低决定；当前子集即使全部命中，也
    不能宣称满足产品文档中以 28 条案例为分母的验收目标。冲突安全率和禁止根因计数只聚合显式
    标注案例，避免把普通超时或权限失败误算为成功响应事实冲突。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["golden-diagnosis-eval:v10"]
    metric_kind: Literal["measured"] = "measured"
    case_count: int = Field(ge=1)
    target_case_count: int = Field(default=GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT, ge=1)
    case_coverage_rate: float = Field(ge=0, le=1)
    target_coverage_complete: bool
    category_case_counts: dict[GoldenCaseCategory, int]
    category_target_counts: dict[GoldenCaseCategory, int]
    intent_accuracy: float = Field(ge=0, le=1)
    root_cause_top1_hit_rate: float = Field(ge=0, le=1)
    necessary_action_coverage: float = Field(ge=0, le=1)
    evidence_source_coverage: float = Field(ge=0, le=1)
    fault_path_completeness: float = Field(ge=0, le=1)
    stop_reason_hit_rate: float = Field(ge=0, le=1)
    citation_completeness: float = Field(ge=0, le=1)
    unsupported_critical_claim_rate: float = Field(ge=0, le=1)
    duplicate_action_rate: float = Field(ge=0, le=1)
    tool_attempt_success_rate: float = Field(ge=0, le=1)
    risk_level_hit_rate: float = Field(ge=0, le=1)
    safe_degradation_rate: float = Field(ge=0, le=1)
    evidence_conflict_safe_resolution_rate: float = Field(ge=0, le=1)
    forbidden_conflict_root_hit_count: int = Field(ge=0)
    history_trigger_hit_rate: float = Field(ge=0, le=1)
    history_recall_coverage: float = Field(ge=0, le=1)
    confirmed_only_recall_rate: float = Field(ge=0, le=1)
    history_projection_pass_rate: float = Field(ge=0, le=1)
    realtime_priority_pass_rate: float = Field(ge=0, le=1)
    forbidden_memory_hit_count: int = Field(ge=0)
    accepted_report_rate: float = Field(ge=0, le=1)
    cases: list[GoldenDiagnosisCaseResult] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_coverage(self) -> GoldenDiagnosisEvalReport:
        """从案例明细重算数量、类别配额与覆盖资格，拒绝手工美化子集边界。

        浮点覆盖率允许 JSON 四舍五入造成的极小误差；案例 ID 必须唯一，否则同一容易案例可能被
        重复计数。超过目标条数同样拒绝，因为这意味着产品目标或契约版本应先显式升级。
        """

        if self.case_count != len(self.cases):
            raise ValueError("golden diagnosis case_count must match case details")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("golden diagnosis case IDs must be unique")
        if self.case_count > self.target_case_count:
            raise ValueError("golden diagnosis case_count cannot exceed the versioned target")
        expected_rate = self.case_count / self.target_case_count
        if abs(self.case_coverage_rate - expected_rate) > 1e-6:
            raise ValueError("golden diagnosis case coverage rate is inconsistent")
        if self.target_coverage_complete != (self.case_count == self.target_case_count):
            raise ValueError("golden diagnosis coverage flag is inconsistent")
        actual_category_counts = {category: 0 for category in GoldenCaseCategory}
        for case in self.cases:
            actual_category_counts[case.case_category] += 1
        if self.category_case_counts != actual_category_counts:
            raise ValueError("golden diagnosis category counts must match case details")
        if self.category_target_counts != GOLDEN_DIAGNOSIS_CATEGORY_TARGETS:
            raise ValueError("golden diagnosis category targets must match product design")
        if sum(self.category_target_counts.values()) != self.target_case_count:
            raise ValueError("golden diagnosis category targets must sum to target_case_count")
        if any(
            self.category_case_counts[category] > self.category_target_counts[category]
            for category in GoldenCaseCategory
        ):
            raise ValueError("golden diagnosis category count cannot exceed its target")
        return self


async def evaluate_golden_diagnosis(
    cases: Sequence[GoldenCaseSpec],
    runner: GoldenDiagnosisRunner,
) -> GoldenDiagnosisEvalReport:
    """顺序运行 Golden Cases，并从真实顶层结果计算宏观指标。

    顺序执行让本地演示资源占用和失败定位保持确定；空集合和重复 case ID 在任何外部运行前失败。
    每条运行异常直接传播，防止缺失案例被当作零分或跳过。宏观比例使用逐案例平均，尝试级成功率
    和重复 Action 率则使用全局计数，避免工具较多案例被无意降权。
    """

    if not cases:
        raise ValueError("golden diagnosis evaluation requires at least one case")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("golden diagnosis evaluation case IDs must be unique")

    case_results: list[GoldenDiagnosisCaseResult] = []
    total_tool_attempts = 0
    successful_tool_attempts = 0
    for case in cases:
        # Runner 是唯一允许发生诊断 I/O 的边界；评分阶段只读取返回的强类型公开结果。
        diagnosis = await runner.run(case)
        case_result = score_golden_diagnosis_case(case, diagnosis)
        case_results.append(case_result)
        attempts = diagnosis.react.state.tool_events
        total_tool_attempts += len(attempts)
        successful_tool_attempts += sum(event.response.ok for event in attempts)

    root_results = [
        result.root_cause_top1_hit
        for result in case_results
        if result.root_cause_top1_hit is not None
    ]
    degradation_results = [
        result.safe_degradation_hit
        for result in case_results
        if result.safe_degradation_hit is not None
    ]
    path_results = [
        result.fault_path_completeness
        for result in case_results
        if result.fault_path_completeness is not None
    ]
    memory_results = [
        result for result in case_results if result.history_recall_coverage is not None
    ]
    conflict_results = [
        result
        for result in case_results
        if result.evidence_conflict_safe_resolution is not None
    ]
    total_claims = sum(result.critical_claim_count for result in case_results)
    unsupported_claims = sum(result.unsupported_critical_claim_count for result in case_results)
    total_actions = sum(result.logical_action_count for result in case_results)
    duplicate_actions = sum(result.duplicate_action_count for result in case_results)
    category_counts = {category: 0 for category in GoldenCaseCategory}
    for case in cases:
        category_counts[case.case_category] += 1

    # 空分母仅出现在某类标注暂不存在时；返回 1.0 表示“没有违反项”，而不是声称模型能力完美。
    return GoldenDiagnosisEvalReport(
        contract_id=GOLDEN_DIAGNOSIS_EVAL_CONTRACT_ID,
        case_count=len(case_results),
        target_case_count=GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT,
        case_coverage_rate=len(case_results) / GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT,
        target_coverage_complete=len(case_results) == GOLDEN_DIAGNOSIS_TARGET_CASE_COUNT,
        category_case_counts=category_counts,
        category_target_counts=GOLDEN_DIAGNOSIS_CATEGORY_TARGETS,
        intent_accuracy=_mean([result.intent_hit for result in case_results]),
        root_cause_top1_hit_rate=_mean(root_results),
        necessary_action_coverage=_mean(
            [result.necessary_action_coverage for result in case_results]
        ),
        evidence_source_coverage=_mean(
            [result.evidence_source_coverage for result in case_results]
        ),
        fault_path_completeness=_mean(path_results),
        stop_reason_hit_rate=_mean([result.stop_reason_hit for result in case_results]),
        citation_completeness=(1.0 if total_claims == 0 else 1 - unsupported_claims / total_claims),
        unsupported_critical_claim_rate=(
            0.0 if total_claims == 0 else unsupported_claims / total_claims
        ),
        duplicate_action_rate=(0.0 if total_actions == 0 else duplicate_actions / total_actions),
        tool_attempt_success_rate=(
            1.0 if total_tool_attempts == 0 else successful_tool_attempts / total_tool_attempts
        ),
        risk_level_hit_rate=_mean([result.risk_level_hit for result in case_results]),
        safe_degradation_rate=_mean(degradation_results),
        evidence_conflict_safe_resolution_rate=_mean(
            [result.evidence_conflict_safe_resolution for result in conflict_results]
        ),
        forbidden_conflict_root_hit_count=sum(
            len(result.forbidden_conflict_root_hits) for result in conflict_results
        ),
        history_trigger_hit_rate=_mean([result.history_trigger_hit for result in memory_results]),
        history_recall_coverage=_mean(
            [result.history_recall_coverage for result in memory_results]
        ),
        confirmed_only_recall_rate=_mean(
            [result.confirmed_only_recall for result in memory_results]
        ),
        history_projection_pass_rate=_mean(
            [result.history_projection_complete for result in memory_results]
        ),
        realtime_priority_pass_rate=_mean(
            [result.realtime_priority_preserved for result in memory_results]
        ),
        forbidden_memory_hit_count=sum(
            len(result.forbidden_memory_hits) for result in memory_results
        ),
        accepted_report_rate=_mean([result.report_accepted for result in case_results]),
        cases=case_results,
    )


def score_golden_diagnosis_case(
    case: GoldenCaseSpec,
    diagnosis: DiagnosisRunResult,
) -> GoldenDiagnosisCaseResult:
    """把单条顶层诊断结果映射为可解释的 Golden 命中明细。

    评分只信任实际 ``ToolEvent``、``Evidence`` 与最终审计报告。必要 Action 按工具名去重；同参重复
    只统计 ``attempt=1`` 的逻辑调用，合法瞬时重试不会被误判。引用完整性检查稳定 ID 是否存在，
    不尝试以字符串相似度替代 Auditor 或人工语义审查。冲突评分只使用版本化 source/root 精确标注，
    先验证 Observation 完整，再检查报告克制与 uncertainty，确保“调用成功”不会自动等价于“事实可信”。
    """

    state = diagnosis.react.state
    report = diagnosis.report.state.draft_report
    if report is None:
        raise ValueError("golden diagnosis result requires a final report")

    executed_tools = list(dict.fromkeys(event.tool_name.value for event in state.tool_events))
    required_tools = [tool.value for tool in case.required_tools]
    missing_tools = [tool for tool in required_tools if tool not in executed_tools]
    action_coverage = _coverage(required_tools, executed_tools)

    # attempt=2 表示同一逻辑 Action 的受控瞬时重试；只有新的 attempt=1 才可能构成重复决策。
    logical_action_keys = [
        _action_key(event.tool_name.value, event.request.model_dump_json())
        for event in state.tool_events
        if event.attempt == 1
    ]
    duplicate_count = len(logical_action_keys) - len(set(logical_action_keys))

    top1 = report.root_causes[0].root_cause if report.root_causes else None
    root_hit = None if not case.allowed_root_causes else top1 in case.allowed_root_causes
    observed_sources = list(dict.fromkeys(evidence.source_id for evidence in state.evidence))
    missing_sources = [
        source for source in case.required_evidence_sources if source not in observed_sources
    ]

    # 路径只有同时存在于检索状态并被最终报告 fault_chain 引用，才算真正参与了诊断输出。
    reported_path_refs = {
        reference for step in report.fault_chain for reference in step.evidence_refs
    }
    eligible_paths = [path for path in state.retrieved_paths if path.path_id in reported_path_refs]
    path_scores = [
        _score_fault_path_requirement(requirement, eligible_paths)
        for requirement in case.required_fault_paths
    ]
    matched_path_labels = [
        requirement.path_label
        for requirement, (coverage, _) in zip(case.required_fault_paths, path_scores, strict=True)
        if coverage == 1.0
    ]
    missing_path_labels = [
        requirement.path_label
        for requirement, (coverage, _) in zip(case.required_fault_paths, path_scores, strict=True)
        if coverage < 1.0
    ]
    matched_path_ids = list(
        dict.fromkeys(
            path_id for coverage, path_ids in path_scores if coverage == 1.0 for path_id in path_ids
        )
    )

    # Graph path 与 confirmed memory 是合法引用源，但不会混入实时 Evidence source 覆盖率。
    valid_refs = {evidence.evidence_id for evidence in state.evidence}
    valid_refs.update(path.path_id for path in state.retrieved_paths)
    valid_refs.update(match.memory.memory_id for match in diagnosis.recalled_memories)
    critical_claim_refs = [root.evidence_refs for root in report.root_causes]
    critical_claim_refs.extend(step.evidence_refs for step in report.fault_chain)
    critical_claim_refs.extend(
        step.evidence_refs for step in report.remediation_steps if step.risk_level is RiskLevel.HIGH
    )
    unsupported_claims = sum(
        not refs or any(reference not in valid_refs for reference in refs)
        for refs in critical_claim_refs
    )
    claim_count = len(critical_claim_refs)

    actual_risk = _highest_risk(report.remediation_steps)
    safe_degradation = None
    if not case.allowed_root_causes:
        # 安全降级必须同时克制根因输出并公开不确定性；仅返回空报告不算可解释降级。
        safe_degradation = not report.root_causes and bool(report.uncertainties)

    required_conflict_sources: list[str] = []
    observed_conflict_sources: list[str] = []
    missing_conflict_sources: list[str] = []
    forbidden_conflict_root_hits: list[str] = []
    conflict_uncertainty_disclosed: bool | None = None
    evidence_conflict_safe_resolution: bool | None = None
    if case.evidence_conflict_expectation is not None:
        expectation = case.evidence_conflict_expectation
        required_conflict_sources = list(expectation.conflicting_evidence_sources)
        observed_source_set = set(observed_sources)
        observed_conflict_sources = [
            source for source in required_conflict_sources if source in observed_source_set
        ]
        missing_conflict_sources = [
            source for source in required_conflict_sources if source not in observed_source_set
        ]
        reported_roots = [root.root_cause for root in report.root_causes]
        forbidden_conflict_root_hits = [
            root
            for root in reported_roots
            if root in expectation.forbidden_root_causes
        ]
        conflict_uncertainty_disclosed = bool(report.uncertainties)

        # “成功响应”本身不代表事实一致；安全通过必须先完整观察冲突双方，再克制结论并公开边界。
        no_root_requirement_met = (
            not report.root_causes if expectation.require_no_root_cause else True
        )
        uncertainty_requirement_met = (
            conflict_uncertainty_disclosed
            if expectation.require_uncertainty_disclosure
            else True
        )
        evidence_conflict_safe_resolution = (
            not missing_conflict_sources
            and not forbidden_conflict_root_hits
            and no_root_requirement_met
            and uncertainty_requirement_met
        )

    history_trigger_hit: bool | None = None
    history_recall_coverage: float | None = None
    confirmed_only_recall: bool | None = None
    history_projection_complete: bool | None = None
    realtime_priority_preserved: bool | None = None
    required_memory_ids: list[str] = []
    recalled_memory_ids: list[str] = []
    missing_memory_ids: list[str] = []
    forbidden_memory_hits: list[str] = []
    if case.history_expectation is not None:
        required_memory_ids = [
            memory.memory_id for memory in case.history_expectation.required_memories
        ]
        recalled_memory_ids = [match.memory.memory_id for match in diagnosis.recalled_memories]
        missing_memory_ids = [
            memory_id for memory_id in required_memory_ids if memory_id not in recalled_memory_ids
        ]
        forbidden_memory_hits = [
            memory_id
            for memory_id in recalled_memory_ids
            if memory_id in case.history_expectation.forbidden_memory_ids
        ]
        history_trigger_hit = diagnosis.history_trigger is not HistoryTrigger.NOT_REQUESTED
        history_recall_coverage = _coverage(required_memory_ids, recalled_memory_ids)
        confirmed_only_recall = bool(diagnosis.recalled_memories) and all(
            match.memory.status is MemoryStatus.CONFIRMED for match in diagnosis.recalled_memories
        )
        projected_ids = [similar.case_id for similar in report.similar_cases]
        history_projection_complete = bool(recalled_memory_ids) and (
            projected_ids == recalled_memory_ids
        )

        conflicting_roots = {
            memory.historical_root_cause
            for memory in case.history_expectation.required_memories
            if memory.expect_root_conflict
        }
        reported_roots = [root.root_cause for root in report.root_causes]
        tool_evidence_ids = {
            evidence.evidence_id
            for evidence in state.evidence
            if evidence.source_type is EvidenceSourceType.TOOL
        }
        roots_have_realtime_support = all(
            bool(set(root.evidence_refs) & tool_evidence_ids) for root in report.root_causes
        )
        realtime_priority_preserved = (
            (top1 in case.allowed_root_causes)
            and not (set(reported_roots) & conflicting_roots)
            and roots_have_realtime_support
        )

    return GoldenDiagnosisCaseResult(
        case_id=case.case_id,
        scenario_id=case.scenario_id,
        case_category=case.case_category,
        intent_hit=state.intent == case.expected_intent,
        executed_tools=executed_tools,
        missing_required_tools=missing_tools,
        necessary_action_coverage=action_coverage,
        duplicate_action_count=duplicate_count,
        logical_action_count=len(logical_action_keys),
        duplicate_action_rate=(
            0.0 if not logical_action_keys else duplicate_count / len(logical_action_keys)
        ),
        root_cause_top1_hit=root_hit,
        actual_top1_root_cause=top1,
        observed_evidence_sources=observed_sources,
        missing_evidence_sources=missing_sources,
        evidence_source_coverage=_coverage(case.required_evidence_sources, observed_sources),
        required_fault_path_labels=[path.path_label for path in case.required_fault_paths],
        matched_fault_path_labels=matched_path_labels,
        missing_fault_path_labels=missing_path_labels,
        matched_fault_path_ids=matched_path_ids,
        fault_path_completeness=(
            None if not path_scores else sum(score for score, _ in path_scores) / len(path_scores)
        ),
        stop_reason_hit=state.stop_reason in case.expected_stop_reasons,
        actual_stop_reason=state.stop_reason or "missing",
        citation_completeness=(1.0 if claim_count == 0 else 1 - unsupported_claims / claim_count),
        unsupported_critical_claim_count=unsupported_claims,
        critical_claim_count=claim_count,
        expected_risk_level=case.expected_risk_level,
        actual_risk_level=actual_risk,
        risk_level_hit=actual_risk is case.expected_risk_level,
        safe_degradation_hit=safe_degradation,
        required_conflicting_evidence_sources=required_conflict_sources,
        observed_conflicting_evidence_sources=observed_conflict_sources,
        missing_conflicting_evidence_sources=missing_conflict_sources,
        forbidden_conflict_root_hits=forbidden_conflict_root_hits,
        conflict_uncertainty_disclosed=conflict_uncertainty_disclosed,
        evidence_conflict_safe_resolution=evidence_conflict_safe_resolution,
        history_trigger_hit=history_trigger_hit,
        required_memory_ids=required_memory_ids,
        recalled_memory_ids=recalled_memory_ids,
        missing_required_memory_ids=missing_memory_ids,
        forbidden_memory_hits=forbidden_memory_hits,
        history_recall_coverage=history_recall_coverage,
        confirmed_only_recall=confirmed_only_recall,
        history_projection_complete=history_projection_complete,
        realtime_priority_preserved=realtime_priority_preserved,
        tool_attempt_success_rate=(
            1.0
            if not state.tool_events
            else sum(event.response.ok for event in state.tool_events) / len(state.tool_events)
        ),
        report_accepted=diagnosis.report.outcome is ReportWorkflowOutcome.ACCEPTED,
    )


def _coverage(required: Sequence[object], actual: Sequence[object]) -> float:
    """计算有序标注集合被实际集合覆盖的比例，并处理空标注分母。

    输入顺序只服务失败明细，不影响集合覆盖；空 required 返回 1.0，表示该案例没有此项义务，
    而不是向宏观结果额外注入一个失败。不可哈希输入会显式抛错，暴露调用方契约漂移。
    """

    if not required:
        return 1.0
    actual_set = set(actual)
    return sum(item in actual_set for item in required) / len(required)


def _score_fault_path_requirement(
    requirement: GoldenFaultPathRequirement,
    actual_paths: Sequence[RetrievedPath],
) -> tuple[float, list[str]]:
    """计算一条必要路径在“已检索且已报告”候选中的最佳节点/关系覆盖。

    节点和关系分别按最长有序子序列覆盖，最终取较小值，避免只命中节点名称却使用错误边类型。
    返回所有达到最佳正覆盖率的 path_id 供失败定位；空候选自然得到零分而不是伪造路径。
    """

    scored_paths: list[tuple[str, float]] = []
    for path in actual_paths:
        node_coverage = _ordered_coverage(path.node_ids, requirement.required_node_ids)
        relation_coverage = _ordered_coverage(
            path.relation_types,
            requirement.required_relation_types,
        )
        # 两类结构同时成立才是可解释故障链；取最小值相当于把较弱边界作为瓶颈。
        scored_paths.append((path.path_id, min(node_coverage, relation_coverage)))
    best_coverage = max((coverage for _, coverage in scored_paths), default=0.0)
    matched_ids = sorted(
        path_id for path_id, coverage in scored_paths if coverage == best_coverage and coverage > 0
    )
    return best_coverage, matched_ids


def _ordered_coverage(actual: Sequence[object], required: Sequence[object]) -> float:
    """计算实际序列对必要序列的最长有序子序列覆盖比例。

    实际路径可包含额外中间节点或关系，但不能倒序命中；required 由 Pydantic 保证非空。若内部调用
    违反该不变量则显式失败，避免除零被误解为满分。
    """

    if not required:
        raise ValueError("ordered Golden path coverage requires a non-empty requirement")
    matched = 0
    for item in actual:
        if matched < len(required) and item == required[matched]:
            matched += 1
    return matched / len(required)


def _mean(values: Sequence[bool | float | None]) -> float:
    """计算已过滤指标的算术平均，空集合按无违反项返回 1.0。

    调用方应先去除不适用的 ``None``；函数仍防御性过滤，以免可选逐案指标污染分母。该约定会在
    报告文档解释，不能把空类别的 1.0 当作有样本测得的能力值。
    """

    measured = [float(value) for value in values if value is not None]
    return 1.0 if not measured else sum(measured) / len(measured)


def _action_key(tool_name: str, request_json: str) -> str:
    """组合工具名和规范化 Pydantic JSON，形成同参 Action 的稳定比较键。

    不使用 Python 对象哈希，避免进程随机种子影响结果；请求中的 trace ID 保证跨 run 调用不会被
    误合并，而同一 run 的相同工具与参数会精确命中重复检查。
    """

    return f"{tool_name}\x1f{request_json}"


def _highest_risk(remediation_steps: Sequence[object]) -> RiskLevel:
    """返回报告建议中的最高风险；无建议时按只读/未处置语义归为低风险。

    该函数只读取具有 ``risk_level`` 属性的领域步骤；固定等级序避免依赖枚举字符串字典序。类型
    漂移会以 AttributeError 显式暴露，而不是把未知对象静默当成低风险。
    """

    ranking = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
    if not remediation_steps:
        return RiskLevel.LOW
    return max((step.risk_level for step in remediation_steps), key=ranking.__getitem__)
