"""将 Planner 调查状态确定性投影为第一版结构化 DiagnosisReport。

草稿只使用现有假设、实时 Evidence、GraphRAG Bundle 和已确认案例元数据，不请求模型补写事实。
证据不足时宁可保留 uncertainties 和只读检查步骤，也不会为了报告完整度编造根因或解决方案。
"""

from __future__ import annotations

from app.domain.models import (
    AgentState,
    CaseMemory,
    DiagnosisReport,
    FaultChainStep,
    HypothesisStatus,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
    SimilarCaseReference,
)
from app.reporting.evidence import collect_valid_reference_ids
from app.retrieval.models import GraphEvidenceBundle, KnowledgeNodeType


class DeterministicReportBuilder:
    """从已验证状态创建可重复、无模型副作用的报告草稿。

    同一输入始终产生相同字段顺序和文本，便于 Golden Case 回放；Builder 不负责语义放行，输出
    仍必须经过规则校验与独立 Auditor。它也不写入长期记忆或声称建议已经执行。
    """

    def build(
        self,
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
        history_case_matches: tuple[SimilarCaseReference, ...] = (),
    ) -> DiagnosisReport:
        """把支持充分的假设、完整图路径和知识方案组装为首版报告。

        输入是 Planner 停止后的 AgentState 及可选检索上下文；输出是通过 DiagnosisReport Schema
        的新对象。存在反对证据、悬空引用或未确认假设时不会生成根因，而是记录不确定性；没有
        SOP/solution 时只生成低风险只读检查，避免把通用建议包装成已验证修复。
        """

        valid_refs = collect_valid_reference_ids(
            state,
            evidence_bundle,
            confirmed_case_memories,
        )
        uncertainties: list[str] = []

        # 只把 supported/confirmed 假设提升为根因；candidate/rejected 不能仅凭置信度越级。
        root_causes: list[RootCauseConclusion] = []
        for hypothesis in state.hypotheses:
            if hypothesis.status not in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}:
                continue
            supporting_refs = _stable_valid_refs(hypothesis.supporting_evidence, valid_refs)
            contradicting_refs = _stable_valid_refs(hypothesis.contradicting_evidence, valid_refs)
            if contradicting_refs:
                uncertainties.append(
                    f"假设 {hypothesis.hypothesis_id} 同时存在反对证据，未提升为根因结论。"
                )
                continue
            if not supporting_refs:
                uncertainties.append(
                    f"假设 {hypothesis.hypothesis_id} 缺少当前上下文中的有效支持引用。"
                )
                continue
            root_causes.append(
                RootCauseConclusion(
                    root_cause=hypothesis.candidate_root_cause,
                    confidence=hypothesis.confidence,
                    evidence_refs=supporting_refs,
                )
            )

        fault_chain = _build_fault_chain(state, evidence_bundle)
        remediation_steps = _build_remediation_steps(evidence_bundle)
        if not root_causes:
            uncertainties.append("当前证据不足以形成可审计根因，报告保持降级结论。")
        if evidence_bundle is not None and evidence_bundle.truncated:
            uncertainties.append("GraphRAG 上下文受到预算裁剪，省略候选不能解释为知识库不存在。")
        if history_case_matches:
            uncertainties.append(
                "历史案例解释仅作参考；差异与本次实时 Observation 冲突时以后者为准。"
            )

        # 报告级引用是所有已采纳结论的并集，便于 API 快速展示和 Auditor 检查遗漏。
        evidence_refs = _stable_unique(
            [
                *(ref for cause in root_causes for ref in cause.evidence_refs),
                *(ref for step in fault_chain for ref in step.evidence_refs),
                *(ref for step in remediation_steps for ref in step.evidence_refs),
                *(ref for item in history_case_matches for ref in item.evidence_refs),
            ]
        )
        summary = (
            f"基于 {len(evidence_refs)} 个可追溯引用形成 {len(root_causes)} 项根因结论。"
            if root_causes
            else "现有调查已停止，但证据不足以安全确认根因；请按只读步骤继续核验。"
        )
        risks = _report_risks(remediation_steps)
        return DiagnosisReport(
            summary=summary,
            fault_chain=fault_chain,
            root_causes=root_causes,
            evidence_refs=evidence_refs,
            remediation_steps=remediation_steps,
            risks=risks,
            uncertainties=_stable_unique(uncertainties),
            similar_cases=list(history_case_matches),
        )


def _build_fault_chain(
    state: AgentState,
    evidence_bundle: GraphEvidenceBundle | None,
) -> list[FaultChainStep]:
    """把完整 GraphRAG 路径转换为带 path_id 引用的可公开链路段。

    优先使用包含边来源跨度的预算化 Bundle；若 Bundle 缺失，再使用 AgentState 中的旧路径投影。
    函数不从相邻节点名称推断额外因果，只把已有节点/关系顺序格式化，重复 path_id 只保留一次。
    """

    chain: list[FaultChainStep] = []
    seen_paths: set[str] = set()
    if evidence_bundle is not None:
        for path in evidence_bundle.selected_paths:
            chain.append(
                FaultChainStep(
                    description=_path_description(
                        path.node_ids, [item.value for item in path.relation_types]
                    ),
                    evidence_refs=[path.path_id],
                )
            )
            seen_paths.add(path.path_id)
    for path in state.retrieved_paths:
        if path.path_id in seen_paths:
            continue
        chain.append(
            FaultChainStep(
                description=_path_description(path.node_ids, path.relation_types),
                evidence_refs=[path.path_id],
            )
        )
        seen_paths.add(path.path_id)
    return chain


def _build_remediation_steps(
    evidence_bundle: GraphEvidenceBundle | None,
) -> list[RemediationStep]:
    """从已召回 solution/SOP 节点生成中风险人工建议，否则生成只读检查步骤。

    知识节点内容保持原文并引用自身 evidence_id；建议标记 medium，明确需审批、可回滚和验证，
    但绝不声称系统已执行。没有方案证据时返回 low 风险的只读核对，避免编造具体写操作。
    """

    solution_nodes = []
    if evidence_bundle is not None:
        solution_nodes = [
            node
            for node in evidence_bundle.selected_nodes
            if node.node_type in {KnowledgeNodeType.SOLUTION, KnowledgeNodeType.SOP}
        ]
    if not solution_nodes:
        return [
            RemediationStep(
                order=1,
                action="继续通过已批准的只读 MCP 工具补齐状态、日志或一致性证据。",
                risk_level=RiskLevel.LOW,
                evidence_refs=[],
                prerequisites=["确认资源标识、时间范围、scenario_id 与 trace_id 均属于本次运行。"],
                rollback="只读检查不修改系统状态；若查询异常，停止并保留已有 ToolEvent。",
                verification="新增 Observation 必须生成稳定 evidence_id，并在下一轮重新审计。",
            )
        ]

    steps: list[RemediationStep] = []
    for order, node in enumerate(solution_nodes, start=1):
        steps.append(
            RemediationStep(
                order=order,
                action=node.content,
                risk_level=RiskLevel.MEDIUM,
                evidence_refs=[node.evidence_id],
                prerequisites=["先在隔离或合成环境复核该 SOP 与本次实时 Observation 一致。"],
                rollback="若验证失败，停止后续步骤并恢复变更前配置或数据快照。",
                verification="重新执行对应只读检查，确认原症状消失且未引入新的链路异常。",
            )
        )
    return steps


def _path_description(node_ids: list[str], relation_types: list[str]) -> str:
    """把有序节点和关系格式化为稳定链路文本，并校验两者数量关系。

    N 个节点必须对应 N-1 条边；不变量破坏表示上游 GraphRAG 模型或旧状态损坏，函数显式失败，
    不用截断 zip 掩盖悬空节点。返回文本只重排已有 ID/关系，不新增因果解释。
    """

    if len(node_ids) != len(relation_types) + 1:
        raise ValueError("fault path requires exactly one relation between adjacent nodes")
    parts = [node_ids[0]]
    for relation, node_id in zip(relation_types, node_ids[1:], strict=True):
        parts.extend([f"-[{relation}]->", node_id])
    return " ".join(parts)


def _report_risks(remediation_steps: list[RemediationStep]) -> list[str]:
    """根据草稿中的真实风险枚举生成稳定且不夸大的报告级风险摘要。

    只读检查明确不产生生产写入；存在中/高风险步骤时提醒审批与回滚。函数不从动作文本猜风险，
    防止自然语言关键词改变控制语义，空步骤则说明当前没有可审计执行方案。
    """

    levels = {step.risk_level for step in remediation_steps}
    if not remediation_steps:
        return ["当前未形成可审计修复步骤，不应执行生产变更。"]
    if RiskLevel.HIGH in levels:
        return ["包含高风险人工操作，必须完成证据复核、审批、备份和回滚演练。"]
    if RiskLevel.MEDIUM in levels:
        return ["知识方案仅供人工评审，先在隔离环境验证后再决定是否实施。"]
    return ["当前仅建议只读核验，不执行生产写操作。"]


def _stable_valid_refs(items: list[str], valid_refs: set[str]) -> list[str]:
    """按首次出现顺序保留合法引用，过滤悬空 ID 并去重。

    过滤结果为空由调用方转为 uncertainty，而不是在此伪造默认引用；集合成员判断只验证存在性，
    语义支持关系仍交给策略校验和 Auditor。输入列表不会被修改。
    """

    return _stable_unique([item for item in items if item in valid_refs])


def _stable_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重字符串，保证报告序列化和测试重放稳定。

    set 只做成员检测，返回新列表保留上游证据优先级；该工具不排序 ID，避免改变 Planner 或检索
    已确定的时间/相关性顺序。空输入合法返回空列表。
    """

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
