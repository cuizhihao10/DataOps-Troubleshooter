"""验证确定性报告草稿、引用/风险门禁和保守修订降级策略。

测试只使用合成 Evidence、假设和 GraphRAG Bundle，不调用模型或数据库；重点证明根因不会从
candidate/冲突假设产生，无效语义即使引用存在也被拦截，返工只删除而不创造新事实。

文档 RAG 侧额外锁定"哪些切片可以变成处置建议"：只有 Runbook/SOP 的步骤小节能被提升，复盘改进项、
FAQ 与"禁止操作"小节即使被召回也只能作为证据存在，否则报告会要求运维执行文档明确禁止的操作。
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
    derive_report_risks,
)
from app.retrieval.documents import (
    BundledDocumentChunk,
    DocumentType,
    make_chunk_id,
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
                remediation_risk_level=RiskLevel.MEDIUM,
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


def test_declared_high_risk_solution_node_reaches_the_report_risk_level() -> None:
    """验证高风险处置等级由知识声明驱动，能真正穿过生产报告路径而不是被硬编码成 medium。

    这条断言修补的是一个曾经存在的实现上限：`_build_remediation_steps` 曾把所有知识方案固定为
    medium，于是 `RiskLevel.HIGH` 在生产路径上永远不可达，`derive_report_risks` 的高风险分支是
    死代码，Golden 案例声明的 high 期望也永远不可能命中。测试同时检查报告级风险摘要升级为
    "必须完成证据复核、审批、备份和回滚演练"，因为等级只在字段里正确、文案仍说"仅供人工评审"
    的话，读报告的人拿到的仍是中风险指引。
    """

    bundle = _bundle()
    high_risk_bundle = bundle.model_copy(
        update={
            "selected_nodes": [
                bundle.selected_nodes[0].model_copy(
                    update={"remediation_risk_level": RiskLevel.HIGH}
                )
            ]
        }
    )

    report = DeterministicReportBuilder().build(_state(), evidence_bundle=high_risk_bundle)

    assert report.remediation_steps[0].risk_level is RiskLevel.HIGH
    assert report.remediation_steps[0].evidence_refs == ["kn_solution_wait"]
    assert report.risks == [
        "包含高风险人工操作，必须完成证据复核、审批、备份和回滚演练。",
    ]


def test_bundle_rejects_missing_or_out_of_scope_remediation_risk_declaration() -> None:
    """验证方案节点缺风险声明与事实节点夹带风险声明两个方向都在 Bundle 边界被拒绝。

    双向校验是这条控制语义的结构性保证：缺声明时报告层没有兜底默认值可用，必须显式失败而不是
    静默退回 medium；反向如果允许 symptom/component 之类的事实节点声明风险，任何一条被召回的
    事实证据都能抬高整份报告的风险等级，而它根本不描述"要对生产做什么"。
    """

    node = _bundle().selected_nodes[0]

    with pytest.raises(ValidationError, match="must declare remediation_risk_level"):
        BundledKnowledgeNode(
            **node.model_dump(exclude={"remediation_risk_level"}),
        )
    with pytest.raises(ValidationError, match="only solution and sop nodes"):
        BundledKnowledgeNode(
            **{
                **node.model_dump(),
                "evidence_id": "kn_symptom_wait",
                "node_id": "symptom_wait",
                "node_type": KnowledgeNodeType.SYMPTOM,
            }
        )


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
    """验证一次修订按 claim_path 精确删除被否决结论，最终降级只保留低风险补证步骤。

    unsupported issue 指到 `root_causes[0]`，修订稿应删掉该根因、保留仍有引用支撑的传播链路，
    并留下面向用户的删除说明；degrade 再次清空历史案例和生产建议。修订稿正文不得出现审计问题码
    或"审计要求修订"这类过程陈述：那不是本轮证据支持的事实，第二轮审计会把它当成新的无支撑声明
    再否决一次，使返工预算必然耗尽。摘要与风险都必须由保留内容推导，避免与实际保留的步骤矛盾。
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
    # 被否决的只有 root_causes[0]，仍有引用支撑的链路不能连带删除：空报告会与现有 Observation
    # 自相矛盾，第二轮审计以 evidence_conflict 再否决一次，返工预算必然耗尽。
    assert revised.fault_chain == initial.fault_chain
    assert revised.uncertainties
    assert any("按定位删除" in item for item in revised.uncertainties)
    assert not any("审计要求修订" in item for item in revised.uncertainties)
    assert not any(
        code.value in item for item in revised.uncertainties for code in AuditIssueCode
    )
    # 风险文本与草稿共享同一推导规则，因此它只能来自实际保留步骤的 risk_level 集合。
    assert revised.risks == derive_report_risks(revised.remediation_steps)
    assert degraded.root_causes == []
    assert degraded.similar_cases == []
    assert degraded.remediation_steps[0].risk_level is RiskLevel.LOW
    assert "不得依据本降级报告" in degraded.risks[0]


def test_safe_reviser_keeps_unflagged_claims_and_falls_back_when_path_is_unparseable() -> None:
    """验证精确删除只作用于被指到的条目，路径不可解析时才退回整类删除。

    第一段构造两个根因、只否决第二个：修订稿必须保留第一个，否则模型给出的正确根因会被连带删掉，
    第二轮审计再以 evidence_conflict 否决一次并耗尽返工预算（首次真实模型评测的案例 1 就是如此）。
    第二段用非法路径确认兜底仍然清空根因与链路——无法定位时"删得更多"才是安全方向。第三段确认
    指向 summary 的问题只触发重算：摘要由保留内容推导，删根因反而会制造新的自相矛盾。
    """

    reviser = SafeReportReviser()
    base = DeterministicReportBuilder().build(_state(), evidence_bundle=_bundle())
    extra = RootCauseConclusion(
        root_cause="合成的第二项根因，引用与首项相同的工具证据。",
        confidence=0.7,
        evidence_refs=["ev_tool_001"],
    )
    two_causes = base.model_copy(update={"root_causes": [*base.root_causes, extra]})

    targeted = reviser.revise(
        two_causes,
        (
            AuditIssue(
                code=AuditIssueCode.UNSUPPORTED_CLAIM,
                claim_path="$.root_causes[1]",
                message="合成 Auditor 只否决第二项根因。",
            ),
        ),
        _state(),
        evidence_bundle=_bundle(),
    )
    fallback = reviser.revise(
        two_causes,
        (
            AuditIssue(
                code=AuditIssueCode.UNSUPPORTED_CLAIM,
                claim_path="root_causes[0].evidence_refs[0]",
                message="合成 Auditor 给出无法解析的定位路径。",
            ),
        ),
        _state(),
        evidence_bundle=_bundle(),
    )
    derived_only = reviser.revise(
        two_causes,
        (
            AuditIssue(
                code=AuditIssueCode.UNSUPPORTED_CLAIM,
                claim_path="summary",
                message="合成 Auditor 认为摘要没有回答用户问题。",
            ),
        ),
        _state(),
        evidence_bundle=_bundle(),
    )

    assert [item.root_cause for item in targeted.root_causes] == [
        base.root_causes[0].root_cause
    ]
    assert targeted.fault_chain == base.fault_chain
    assert fallback.root_causes == []
    assert fallback.fault_chain == []
    assert any("引用集合不足" in item for item in fallback.uncertainties)
    assert [item.root_cause for item in derived_only.root_causes] == [
        item.root_cause for item in two_causes.root_causes
    ]
    # 摘要被否决时靠重算解决：新摘要必须复述保留下来的首要根因，而不是描述"报告已被修订"。
    assert base.root_causes[0].root_cause.strip() in derived_only.summary


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


def _document_chunk(
    ordinal: int,
    *,
    doc_type: DocumentType,
    section: str,
) -> BundledDocumentChunk:
    """构造一个指定文档类型与末级小节名的紧凑文档证据，用于建议提升规则断言。

    只有 doc_type 与 heading_path 的末级小节参与判断，因此其余字段固定；`dc_*` 由 `make_chunk_id`
    生成，保证测试引用与生产、数据库主键使用同一套规则。
    """

    doc_id = f"doc_{doc_type.value}_case"
    return BundledDocumentChunk(
        evidence_id=make_chunk_id(doc_id, ordinal),
        chunk_id=make_chunk_id(doc_id, ordinal),
        doc_id=doc_id,
        doc_type=doc_type,
        title=f"合成{doc_type.value}文档",
        heading_path=f"合成{doc_type.value}文档 > {section}",
        content=f"{section}：暂停任务并核对主键分布后再由人工审批恢复。",
        source_id=f"synthetic_{doc_type.value}_case",
        revision="r1",
        reliability=0.9,
        retrieval_score=0.8,
    )


def test_only_runbook_and_sop_step_sections_become_remediation_steps() -> None:
    """验证只有 Runbook/SOP 的步骤小节被提升为处置建议，其它小节仅作为证据存在。

    复盘的"改进项"是长期治理动作、FAQ 是判断依据、Runbook 的"禁止操作"更是明确不能做的事；把它们
    混进建议会让报告要求运维执行一件文档禁止的操作。断言同时检查提升顺序（图节点先于文档切片）、
    风险等级为 medium、引用为 `dc_*`，以及未提升切片的 ID 不出现在报告级引用里。
    """

    bundle = _bundle().model_copy(
        update={
            "selected_documents": [
                _document_chunk(0, doc_type=DocumentType.RUNBOOK, section="处置步骤"),
                _document_chunk(1, doc_type=DocumentType.RUNBOOK, section="禁止操作"),
                _document_chunk(0, doc_type=DocumentType.SOP, section="确认步骤"),
                _document_chunk(0, doc_type=DocumentType.POSTMORTEM, section="改进项"),
                _document_chunk(0, doc_type=DocumentType.FAQ, section="常见问题"),
            ]
        }
    )

    report = DeterministicReportBuilder().build(_state(), evidence_bundle=bundle)

    promoted = [step.evidence_refs[0] for step in report.remediation_steps]
    assert promoted == [
        "kn_solution_wait",
        make_chunk_id("doc_runbook_case", 0),
        make_chunk_id("doc_sop_case", 0),
    ]
    assert [step.order for step in report.remediation_steps] == [1, 2, 3]
    assert all(step.risk_level is RiskLevel.MEDIUM for step in report.remediation_steps)
    assert "修订版本 r1" in report.remediation_steps[1].prerequisites[0]
    # 未提升的切片仍是合法引用来源，但不能出现在报告级引用里——那会暗示它支撑了某项结论。
    assert make_chunk_id("doc_runbook_case", 1) not in report.evidence_refs
    assert make_chunk_id("doc_postmortem_case", 0) not in report.evidence_refs
    assert make_chunk_id("doc_faq_case", 0) not in report.evidence_refs


def test_document_chunks_alone_can_carry_remediation_without_solution_nodes() -> None:
    """验证没有 solution/SOP 图节点时，Runbook 步骤切片仍能产出中风险建议而非只读兜底。

    这是文档 RAG 存在的直接理由：知识图能解释传播链，但可执行步骤只写在 Runbook 里。若实现只在
    图节点存在时才看文档，文档通道对最终建议就毫无贡献，检索指标再好也不改变报告内容。
    """

    bundle = _bundle().model_copy(
        update={
            "selected_nodes": [],
            "selected_documents": [
                _document_chunk(0, doc_type=DocumentType.RUNBOOK, section="恢复步骤")
            ],
        }
    )

    report = DeterministicReportBuilder().build(_state(), evidence_bundle=bundle)

    assert len(report.remediation_steps) == 1
    step = report.remediation_steps[0]
    assert step.evidence_refs == [make_chunk_id("doc_runbook_case", 0)]
    assert step.risk_level is RiskLevel.MEDIUM
    assert "只读" not in step.action
