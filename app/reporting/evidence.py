"""集中计算报告与 Auditor 可以引用的证据 ID 集合和来源分类。

同一规则同时被草稿、策略校验和修订使用，避免三个节点各自解释 Evidence/GraphRAG/案例来源。
函数只读取强类型对象，不访问数据库或模型，也不会把相似度分数当作事实证据。
"""

from __future__ import annotations

from app.domain.models import AgentState, CaseMemory, EvidenceSourceType
from app.retrieval.models import GraphEvidenceBundle


def collect_reference_sources(
    state: AgentState,
    evidence_bundle: GraphEvidenceBundle | None,
    confirmed_case_memories: tuple[CaseMemory, ...],
) -> dict[str, EvidenceSourceType]:
    """返回当前报告可引用 ID 到证据来源类型的稳定映射。

    实时 Evidence 优先写入，GraphRAG 节点/路径随后补充，最后才加入已确认案例携带的历史引用；
    同一 ID 若跨来源冲突会抛出 ValueError，防止历史记录覆盖本次 Observation。输入案例必须由
    上游确认状态门禁保证为 confirmed，本函数只建立引用索引，不静默过滤污染数据。
    """

    sources: dict[str, EvidenceSourceType] = {}
    for evidence in state.evidence:
        _insert_source(sources, evidence.evidence_id, evidence.source_type)
    for path in state.retrieved_paths:
        _insert_source(sources, path.path_id, EvidenceSourceType.GRAPH_PATH)
    if evidence_bundle is not None:
        # Bundle 节点使用独立 evidence_id；路径的 evidence_id 与 path_id 按契约相同。
        for node in evidence_bundle.selected_nodes:
            _insert_source(sources, node.evidence_id, EvidenceSourceType.KNOWLEDGE_NODE)
        for path in evidence_bundle.selected_paths:
            _insert_source(sources, path.evidence_id, EvidenceSourceType.GRAPH_PATH)
    for memory in confirmed_case_memories:
        # 案例本身是可审计历史来源；其内部 evidence_refs 没有随原 Evidence 内容注入时不能伪装
        # 成本次可直接核对的证据。报告应引用 memory_id，再由案例对象追溯其历史引用。
        _insert_source(sources, memory.memory_id, EvidenceSourceType.CASE_MEMORY)
    return sources


def collect_valid_reference_ids(
    state: AgentState,
    evidence_bundle: GraphEvidenceBundle | None,
    confirmed_case_memories: tuple[CaseMemory, ...],
) -> set[str]:
    """返回报告、审计和修订共享的全部合法 evidence_id/path_id 集合。

    本函数委托来源索引完成冲突检测，再复制键集合供成员判断；调用方不能修改来源映射，也无需
    了解不同证据容器的字段差异。空状态合法返回空集合，后续报告必须通过 uncertainties 降级。
    """

    return set(collect_reference_sources(state, evidence_bundle, confirmed_case_memories))


def _insert_source(
    sources: dict[str, EvidenceSourceType],
    reference_id: str,
    source_type: EvidenceSourceType,
) -> None:
    """插入一个引用来源，并拒绝同一稳定 ID 被解释为两种来源。

    完全相同的重放保持首个值；来源冲突表示 ID 命名或上游持久化契约损坏，必须显式失败而不能
    依靠字典覆盖继续生成报告。该辅助函数没有返回值，成功即表示索引仍保持一致。
    """

    current = sources.get(reference_id)
    if current is not None and current is not source_type:
        raise ValueError(f"conflicting evidence source for {reference_id}")
    sources.setdefault(reference_id, source_type)
