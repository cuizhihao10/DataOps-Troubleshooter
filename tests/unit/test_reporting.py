"""验证确定性报告草稿、引用/风险门禁和保守修订降级策略。

测试只使用合成 Evidence、假设和 GraphRAG Bundle，不调用模型或数据库；重点证明根因不会从
candidate/冲突假设产生，无效语义即使引用存在也被拦截，返工只删除而不创造新事实。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    AuditResult,
    AuditStatus,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
)
from app.reporting import (
    DeterministicReportBuilder,
    ReportPolicyValidator,
    SafeReportReviser,
)
from app.retrieval.models import (
    BundledGraphPath,
    BundledKnowledgeNode,
    EvidenceBundleBudget,
    GraphEvidenceBundle,
    KnowledgeNodeType,
    KnowledgeRelationType,
    RetrievalMode,
)

OBSERVED_AT = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)


def _state() -> AgentState:
    """构造一个拥有实时 Evidence、支持根因和候选根因的合成终态。

    supported 假设引用真实工具证据，candidate 使用相同引用但不得被 Builder 提升；stop_reason
    表示 ReAct 已完成。返回对象不包含 Thought、报告或审计结果。
    """

    evidence = Evidence(
        evidence_id="ev_tool_001",
        source_type=EvidenceSourceType.TOOL,
        source_id="synthetic_lts_status",
        content="合成任务状态显示上游数据未就绪。",
        observed_at=OBSERVED_AT,
        reliability=0.95,
    )
    return AgentState(
        run_id="run_reporting_unit_001",
        session_id="session_reporting_unit_001",
        user_query="检查合成任务失败链路",
        evidence=[evidence],
        observation_refs=[evidence.evidence_id],
        hypotheses=[
            FaultHypothesis(
                hypothesis_id="hyp_supported",
                symptom="任务等待上游",
                candidate_root_cause="上游数据未按时就绪",
                components=[Component.LTS],
                supporting_evidence=[evidence.evidence_id],
                status=HypothesisStatus.SUPPORTED,
                confidence=0.8,
            ),
            FaultHypothesis(
                hypothesis_id="hyp_candidate",
                symptom="任务失败",
                candidate_root_cause="尚未验证的资源不足",
                components=[Component.BDS],
                supporting_evidence=[evidence.evidence_id],
                status=HypothesisStatus.CANDIDATE,
                confidence=0.9,
            ),
        ],
        stop_reason="evidence_sufficient",
    )


def _bundle() -> GraphEvidenceBundle:
    """构造包含一条完整路径和一个 solution 节点的预算化 GraphRAG Bundle。

    路径提供 fault_chain 的 path_id，solution 节点提供修复建议引用；所有内容均为合成数据。预算
    数值只用于模型构造，本测试不重复验证 budget builder 的精确字节算法。
    """

    return GraphEvidenceBundle(
        query="合成任务上游数据未就绪",
        retrieval_mode=RetrievalMode.HYBRID_GRAPH,
        budget=EvidenceBundleBudget(max_bytes=6000, max_nodes=8, max_paths=4),
        used_bytes=512,
        selected_nodes=[
            BundledKnowledgeNode(
                evidence_id="kn_solution_wait",
                node_id="solution_wait",
                node_type=KnowledgeNodeType.SOLUTION,
                name="人工补齐上游数据后复核",
                content="先在隔离环境确认上游数据完整，再由人工审批是否恢复下游调度。",
                source_id="synthetic_sop_wait",
                source_span="合成 SOP 第 1 段",
                reliability=0.8,
                retrieval_score=0.75,
            )
        ],
        selected_paths=[
            BundledGraphPath(
                evidence_id="path_0123456789abcdef",
                path_id="path_0123456789abcdef",
                seed_node_id="task_lts",
                node_ids=["task_lts", "symptom_wait", "cause_upstream"],
                edge_ids=["edge_manifest", "edge_cause"],
                relation_types=[
                    KnowledgeRelationType.MANIFESTS_AS,
                    KnowledgeRelationType.CAUSED_BY,
                ],
                edge_source_spans=["合成路径边 1", "合成路径边 2"],
                source_ids=["synthetic_graph_source"],
                depth=2,
                path_score=0.72,
                hybrid_score=0.79,
            )
        ],
    )


def _readonly_step() -> RemediationStep:
    """构造字段完整的低风险只读核验步骤，供手工报告测试复用。

    步骤没有证据引用，因为它只要求补证而非声称某个修复必要；前置、回滚和验证均显式存在，
    确保测试失败聚焦根因支撑而非风险字段缺失。
    """

    return RemediationStep(
        order=1,
        action="继续只读核验。",
        risk_level=RiskLevel.LOW,
        prerequisites=["确认本次 run_id。"],
        rollback="不修改系统状态。",
        verification="记录新的 Evidence。",
    )


def test_deterministic_builder_uses_only_supported_claims_and_cited_graph_content() -> None:
    """验证 Builder 只提升 supported 假设，并为链路和知识方案保留真实引用。

    candidate 即使置信度更高也不能进入根因；GraphRAG path_id 与 solution evidence_id 必须分别
    出现在链路/修复和报告级引用中，证明草稿不是自由文本拼接。
    """

    report = DeterministicReportBuilder().build(_state(), evidence_bundle=_bundle())

    assert [item.root_cause for item in report.root_causes] == ["上游数据未按时就绪"]
    assert report.root_causes[0].evidence_refs == ["ev_tool_001"]
    assert report.fault_chain[0].evidence_refs == ["path_0123456789abcdef"]
    assert report.remediation_steps[0].evidence_refs == ["kn_solution_wait"]
    assert report.remediation_steps[0].risk_level is RiskLevel.MEDIUM
    assert report.evidence_refs == [
        "ev_tool_001",
        "path_0123456789abcdef",
        "kn_solution_wait",
    ]


def test_policy_vetoes_valid_but_semantically_unsupported_root_cause() -> None:
    """验证存在真实 evidence_id 仍不足以支持未对应假设的根因文本。

    报告引用可寻址工具证据，但 root_cause 不匹配任何 supported/confirmed 假设；Validator 应返回
    unsupported_claim，证明门禁不只检查“引用列表非空”。
    """

    report = DiagnosisReport(
        summary="人为注入的无依据结论。",
        root_causes=[
            RootCauseConclusion(
                root_cause="并不存在的数据库损坏",
                confidence=0.99,
                evidence_refs=["ev_tool_001"],
            )
        ],
        evidence_refs=["ev_tool_001"],
        remediation_steps=[_readonly_step()],
        risks=["仅只读核验。"],
    )

    issues = ReportPolicyValidator().validate(report, _state())

    assert [issue.code for issue in issues] == [AuditIssueCode.UNSUPPORTED_CLAIM]
    assert issues[0].claim_path == "root_causes[0]"


def test_safe_reviser_removes_unsupported_claim_and_degrade_never_confirms_root_cause() -> None:
    """验证一次修订和最终降级都通过删除结论收窄风险，而不是改写新根因。

    同一 unsupported issue 进入 revise 后应移除根因/链路并添加 uncertainty；degrade 再次清空
    历史案例和生产建议，只保留低风险补证步骤。
    """

    initial = DeterministicReportBuilder().build(_state(), evidence_bundle=_bundle())
    issue = AuditIssue(
        code=AuditIssueCode.UNSUPPORTED_CLAIM,
        claim_path="root_causes[0]",
        message="合成 Auditor 判断引用内容不足以支持根因。",
        evidence_refs=("ev_tool_001",),
    )
    reviser = SafeReportReviser()

    revised = reviser.revise(initial, (issue,), _state(), evidence_bundle=_bundle())
    degraded = reviser.degrade(revised, (issue,), _state(), evidence_bundle=_bundle())

    assert revised.root_causes == []
    assert revised.fault_chain == []
    assert any("unsupported_claim" in item for item in revised.uncertainties)
    assert degraded.root_causes == []
    assert degraded.similar_cases == []
    assert degraded.remediation_steps[0].risk_level is RiskLevel.LOW
    assert "不得依据本降级报告" in degraded.risks[0]


def test_domain_schema_rejects_uncontrolled_high_risk_and_contradictory_audit_payloads() -> None:
    """验证 Pydantic 在工作流前拒绝无依据 high 建议和矛盾 accept/revise 组合。

    高风险步骤缺 evidence/prerequisites 时不能创建；accept 携带 issue、revise 没有问题也不能创建，
    防止模型或旧 checkpoint 绕过 LangGraph 条件路由。
    """

    with pytest.raises(ValidationError):
        RemediationStep(
            order=1,
            action="执行高风险生产变更。",
            risk_level=RiskLevel.HIGH,
            rollback="恢复快照。",
            verification="重新检查。",
        )
    issue = AuditIssue(
        code=AuditIssueCode.REPORT_INCOMPLETE,
        claim_path="uncertainties",
        message="缺少降级说明。",
    )
    with pytest.raises(ValidationError):
        AuditResult(status=AuditStatus.ACCEPT, issues=[issue])
    with pytest.raises(ValidationError):
        AuditResult(
            status=AuditStatus.REVISE,
            revision_instructions=["补充说明。"],
        )
