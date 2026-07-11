"""以确定性规则审查报告引用、假设支撑、冲突、风险保护和案例状态。

这些规则不替代 Auditor 的语义判断，但拥有最终否决权：即使模型返回 accept，任何客观不变量
失败仍必须进入 revise。规则只产生 AuditIssue，不修改报告，保持验证与修订职责分离。
"""

from __future__ import annotations

from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    CaseMemory,
    DiagnosisReport,
    HypothesisStatus,
    RiskLevel,
    SimilarCaseReference,
)
from app.reporting.evidence import collect_valid_reference_ids
from app.retrieval.models import GraphEvidenceBundle


class ReportPolicyValidator:
    """对 DiagnosisReport 执行供应商无关、可重复的放行前检查。

    Validator 不调用模型或数据库；它把所有客观问题一次性收集，减少返工往返。自然语言是否真正
    支持结论仍由 Auditor 判断，但根因必须能对应状态中的 supported/confirmed 假设。
    """

    def validate(
        self,
        report: DiagnosisReport,
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
        history_case_matches: tuple[SimilarCaseReference, ...] = (),
    ) -> tuple[AuditIssue, ...]:
        """返回所有阻断放行的结构化问题，空元组表示确定性规则通过。

        校验顺序先建立合法引用，再检查报告级集合、根因/链路/步骤/案例，最后检查降级完整性；
        这样每条错误都能定位原始字段。函数不会因首个错误提前返回，也不会把模型 accept 当作豁免。
        """

        # 先建立唯一来源索引；ID 来源冲突属于上游契约损坏，必须在收集规则问题前显式失败。
        valid_refs = collect_valid_reference_ids(
            state,
            evidence_bundle,
            confirmed_case_memories,
        )
        issues: list[AuditIssue] = []
        # 按存在性、语义支撑、风险、案例和完整性顺序收集，便于返工从基础引用开始处理。
        issues.extend(_invalid_reference_issues(report, valid_refs))
        issues.extend(_root_cause_issues(report, state, valid_refs))
        issues.extend(_risk_control_issues(report))
        issues.extend(_case_issues(report, confirmed_case_memories, history_case_matches))
        issues.extend(_completeness_issues(report))
        return tuple(_deduplicate_issues(issues))


def _invalid_reference_issues(
    report: DiagnosisReport,
    valid_refs: set[str],
) -> list[AuditIssue]:
    """检查报告级及各项结论引用是否都存在，并检查汇总引用没有漏项。

    每个字段单独报告 claim_path，便于安全修订精确删除问题项；报告级 evidence_refs 还必须覆盖
    所有已采纳子项引用，否则 API 汇总会误导用户。函数不判断引用语义，只检查可寻址性和完整性。
    """

    issues: list[AuditIssue] = []
    claim_refs: list[str] = []
    groups: list[tuple[str, list[str]]] = [("evidence_refs", report.evidence_refs)]
    groups.extend(
        (f"root_causes[{index}].evidence_refs", item.evidence_refs)
        for index, item in enumerate(report.root_causes)
    )
    groups.extend(
        (f"fault_chain[{index}].evidence_refs", item.evidence_refs)
        for index, item in enumerate(report.fault_chain)
    )
    groups.extend(
        (f"remediation_steps[{index}].evidence_refs", item.evidence_refs)
        for index, item in enumerate(report.remediation_steps)
    )
    groups.extend(
        (f"similar_cases[{index}].evidence_refs", item.evidence_refs)
        for index, item in enumerate(report.similar_cases)
    )
    for path, refs in groups:
        invalid = tuple(ref for ref in refs if ref not in valid_refs)
        if invalid:
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.INVALID_EVIDENCE_REF,
                    claim_path=path,
                    message="报告引用了当前 Evidence、GraphRAG 或已确认案例中不存在的 ID。",
                    evidence_refs=invalid,
                )
            )
        if path != "evidence_refs":
            claim_refs.extend(refs)
    missing_from_summary = tuple(
        ref for ref in _stable_unique(claim_refs) if ref not in report.evidence_refs
    )
    if missing_from_summary:
        issues.append(
            AuditIssue(
                code=AuditIssueCode.REPORT_INCOMPLETE,
                claim_path="evidence_refs",
                message="报告级 evidence_refs 未覆盖全部已采纳结论引用。",
                evidence_refs=missing_from_summary,
            )
        )
    return issues


def _root_cause_issues(
    report: DiagnosisReport,
    state: AgentState,
    valid_refs: set[str],
) -> list[AuditIssue]:
    """要求每项根因对应支持/确认假设，并拦截仍有有效反对证据的结论。

    根因文本用精确匹配连接 FaultHypothesis，避免模糊字符串把不同故障合并；支持引用必须与假设
    的 supporting_evidence 有交集。contradicting_evidence 一旦仍有效，就要求 revise 而非比较分数。
    """

    issues: list[AuditIssue] = []
    hypotheses = {
        item.candidate_root_cause: item
        for item in state.hypotheses
        if item.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}
    }
    for index, conclusion in enumerate(report.root_causes):
        path = f"root_causes[{index}]"
        hypothesis = hypotheses.get(conclusion.root_cause)
        if hypothesis is None:
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.UNSUPPORTED_CLAIM,
                    claim_path=path,
                    message="根因未对应当前状态中的 supported/confirmed 假设。",
                    evidence_refs=tuple(conclusion.evidence_refs),
                )
            )
            continue
        valid_supporting = set(hypothesis.supporting_evidence) & valid_refs
        if not valid_supporting.intersection(conclusion.evidence_refs):
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.UNSUPPORTED_CLAIM,
                    claim_path=path,
                    message="根因引用未命中对应假设的有效 supporting_evidence。",
                    evidence_refs=tuple(conclusion.evidence_refs),
                )
            )
        conflicts = tuple(ref for ref in hypothesis.contradicting_evidence if ref in valid_refs)
        if conflicts:
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.EVIDENCE_CONFLICT,
                    claim_path=path,
                    message="该根因仍存在有效反对证据，不能以确定结论放行。",
                    evidence_refs=conflicts,
                )
            )
    return issues


def _risk_control_issues(report: DiagnosisReport) -> list[AuditIssue]:
    """检查每项修复建议都有前置条件，高风险建议还有有效依据。

    Schema 已强制 rollback/verification 非空并在 high 时要求引用；此处再次检查前置条件和报告级
    语义，防止从旧 checkpoint 或未来迁移绕过当前模型。检查只读字段，不执行任何修复动作。
    """

    issues: list[AuditIssue] = []
    for index, step in enumerate(report.remediation_steps):
        missing_controls = not step.prerequisites
        if step.risk_level is RiskLevel.HIGH and not step.evidence_refs:
            missing_controls = True
        if missing_controls:
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.MISSING_RISK_CONTROL,
                    claim_path=f"remediation_steps[{index}]",
                    message="修复建议缺少前置条件，或高风险操作缺少支持其必要性的证据。",
                    evidence_refs=tuple(step.evidence_refs),
                )
            )
    return issues


def _case_issues(
    report: DiagnosisReport,
    confirmed_case_memories: tuple[CaseMemory, ...],
    history_case_matches: tuple[SimilarCaseReference, ...],
) -> list[AuditIssue]:
    """拒绝未知案例，或与确定性历史解释在分数和字段上发生漂移的报告。

    `confirmed` 布尔值不足以证明来源，case_id 必须命中同批 memory；报告项还必须与 matcher 的
    完整强类型结果相同，防止后续节点提高 similarity、删除冲突差异或改写历史方案。
    """

    confirmed_ids = {memory.memory_id for memory in confirmed_case_memories}
    expected_by_id = {item.case_id: item for item in history_case_matches}
    issues: list[AuditIssue] = []
    for index, item in enumerate(report.similar_cases):
        if not item.confirmed or item.case_id not in confirmed_ids:
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.UNCONFIRMED_CASE,
                    claim_path=f"similar_cases[{index}]",
                    message="相似案例未出现在本轮已确认案例上下文中。",
                    evidence_refs=tuple(item.evidence_refs),
                )
            )
            continue
        expected = expected_by_id.get(item.case_id)
        if expected is None or item != expected:
            issues.append(
                AuditIssue(
                    code=AuditIssueCode.EVIDENCE_CONFLICT,
                    claim_path=f"similar_cases[{index}]",
                    message="报告中的相似度或案例解释与确定性历史匹配结果不一致。",
                    evidence_refs=tuple(item.evidence_refs),
                )
            )
    return issues


def _completeness_issues(report: DiagnosisReport) -> list[AuditIssue]:
    """保证无根因的报告明确声明不确定性，且报告始终保留人工下一步。

    证据不足是合法结果，但空 root_causes 不能同时用空 uncertainties 假装完整；同理，没有任何
    remediation step 时必须解释原因。规则不要求虚构根因，只要求降级语义可见。
    """

    issues: list[AuditIssue] = []
    if not report.root_causes and not report.uncertainties:
        issues.append(
            AuditIssue(
                code=AuditIssueCode.REPORT_INCOMPLETE,
                claim_path="uncertainties",
                message="无根因报告必须明确证据缺口或无法判断原因。",
            )
        )
    if not report.remediation_steps and not report.uncertainties:
        issues.append(
            AuditIssue(
                code=AuditIssueCode.REPORT_INCOMPLETE,
                claim_path="remediation_steps",
                message="没有修复建议时必须解释为什么只能安全降级。",
            )
        )
    return issues


def _deduplicate_issues(issues: list[AuditIssue]) -> list[AuditIssue]:
    """按代码、字段路径和引用组合稳定去重审计问题。

    message 不参与键，避免同一客观问题因措辞差异重复消耗上下文；首次问题保留，输出顺序与校验
    阶段一致，便于测试和 UI 展示。函数返回新列表，不修改调用方集合。
    """

    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    result: list[AuditIssue] = []
    for issue in issues:
        key = (issue.code.value, issue.claim_path, issue.evidence_refs)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _stable_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重引用，用于检查报告级汇总是否完整。

    与报告 Builder 保持相同顺序语义，但局部实现避免策略模块依赖 Builder 私有函数。输入和输出
    都是普通字符串列表，空输入合法返回空列表。
    """

    return list(dict.fromkeys(items))
