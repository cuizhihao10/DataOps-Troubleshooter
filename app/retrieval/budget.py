"""将完整 GraphRAG 与文档检索结果裁剪成受 UTF-8 JSON、节点数、路径数和切片数约束的证据 Bundle。

预算选择是确定性基础设施，不交给 LLM 自行摘要或删除证据。算法优先按检索排序原子加入完整路径
及其所有节点，再补充未出现的高分种子，最后按最终分加入文档切片；任何候选如果会突破任一预算就
整体省略并记录稳定 ID，从而保证 Planner 看不到断裂路径，也能知道上下文因预算而不完整。

文档切片排在图证据之后，是因为图路径是本系统区别于普通 RAG 的可解释部分：预算紧张时应先保住
"故障如何沿依赖传播"，再补"处置步骤写在哪一节"，而不是让几段长文档把关系证据整体挤出上下文。
"""

from __future__ import annotations

import json

from app.retrieval.documents import (
    BundledDocumentChunk,
    DocumentRetrievalResult,
    ScoredDocumentChunk,
)
from app.retrieval.models import (
    BundledGraphPath,
    BundledKnowledgeNode,
    EvidenceBundleBudget,
    GraphEvidenceBundle,
    GraphRetrievalResult,
    KnowledgeNode,
    ScoredGraphPath,
)

# 知识节点引用前缀单列成常量，因为它已经不只是显示格式：`kn_<node_id>` 让一条报告引用精确编码知识图
# 节点 ID，评测侧因此可以离线反解出"报告引用了哪个 root_cause 节点"。两处各写一份字面量会在改前缀的
# 那天让反解静默失配，指标退化成恒为 0 而不是报错。
KNOWLEDGE_EVIDENCE_ID_PREFIX = "kn_"


def build_evidence_bundle(
    result: GraphRetrievalResult,
    *,
    budget: EvidenceBundleBudget,
    documents: DocumentRetrievalResult | None = None,
) -> GraphEvidenceBundle:
    """按完整路径优先策略构造从不超过四重预算的 GraphEvidenceBundle。

    每条路径与尚未选择的路径节点作为一个原子候选，只有节点数、路径数和规范 JSON 字节数都满足
    才纳入；随后按种子混合分补充独立节点，最后在剩余字节里加入文档切片。所有候选 ID 最终分成
    selected 或 omitted，两边不重叠。空检索结果合法返回只含规范空列表包装的最小主体，输入结果
    本身不会被修改；`documents` 为空表示本次没有文档通道，与"文档通道未召回"在 Bundle 里同形，
    差异由检索事件而不是证据主体表达。
    """

    node_scores = _collect_node_scores(result)
    node_candidates = _collect_node_candidates(result, node_scores=node_scores)
    selected_nodes: dict[str, BundledKnowledgeNode] = {}
    selected_paths: list[BundledGraphPath] = []

    # 路径顺序沿用检索服务的最终混合分排序；每条路径必须连同全部节点原子进入上下文。
    for path in result.paths:
        if len(selected_paths) >= budget.max_paths:
            continue
        path_item = _bundle_path(path)
        path_nodes = {
            node.node_id: node_candidates[node.node_id]
            for node in path.nodes
            if node.node_id not in selected_nodes
        }
        proposed_nodes = [*selected_nodes.values(), *path_nodes.values()]
        proposed_paths = [*selected_paths, path_item]
        if len(proposed_nodes) > budget.max_nodes:
            continue
        if _payload_size(proposed_nodes, proposed_paths, []) > budget.max_bytes:
            continue
        selected_nodes.update(path_nodes)
        selected_paths.append(path_item)

    # 路径节点完成去重后，再按种子排名补充孤立但高相关的知识证据。
    for seed in result.seeds:
        if seed.node.node_id in selected_nodes:
            continue
        if len(selected_nodes) >= budget.max_nodes:
            break
        candidate = node_candidates[seed.node.node_id]
        proposed_nodes = [*selected_nodes.values(), candidate]
        if _payload_size(proposed_nodes, selected_paths, []) > budget.max_bytes:
            continue
        selected_nodes[seed.node.node_id] = candidate

    chunk_candidates = list(documents.chunks) if documents is not None else []
    selected_documents: list[BundledDocumentChunk] = []
    for chunk in chunk_candidates:
        if len(selected_documents) >= budget.max_documents:
            break
        chunk_item = _bundle_chunk(chunk)
        proposed_documents = [*selected_documents, chunk_item]
        if (
            _payload_size(list(selected_nodes.values()), selected_paths, proposed_documents)
            > budget.max_bytes
        ):
            continue
        selected_documents.append(chunk_item)

    selected_node_ids = set(selected_nodes)
    selected_path_ids = {path.path_id for path in selected_paths}
    selected_chunk_ids = {chunk.chunk_id for chunk in selected_documents}
    all_node_ids = set(node_candidates)
    all_path_ids = {path.path_id for path in result.paths}
    all_chunk_ids = {chunk.chunk.chunk_id for chunk in chunk_candidates}
    omitted_node_ids = sorted(all_node_ids - selected_node_ids)
    omitted_path_ids = sorted(all_path_ids - selected_path_ids)
    omitted_chunk_ids = sorted(all_chunk_ids - selected_chunk_ids)
    used_bytes = _payload_size(list(selected_nodes.values()), selected_paths, selected_documents)

    return GraphEvidenceBundle(
        query=result.query,
        retrieval_mode=result.mode,
        budget=budget,
        used_bytes=used_bytes,
        selected_nodes=list(selected_nodes.values()),
        selected_paths=selected_paths,
        selected_documents=selected_documents,
        omitted_node_ids=omitted_node_ids,
        omitted_path_ids=omitted_path_ids,
        omitted_chunk_ids=omitted_chunk_ids,
        truncated=bool(omitted_node_ids or omitted_path_ids or omitted_chunk_ids),
    )


def _collect_node_scores(result: GraphRetrievalResult) -> dict[str, float]:
    """为所有种子和路径节点计算它们在本次检索中的最高可解释优先分。

    优先分统一使用 `final_score`：它在只跑一阶段时等于 `hybrid_score`，启用 cross-encoder 后又能
    让精排结论真正影响进入上下文的证据，而不是只改变返回列表的显示顺序。种子使用自身分数，非种子
    路径节点继承包含它的最高路径分；同一节点出现多次时取最大值，既保证稳定排序信息，又不把多次
    出现机械累加成更强事实。该分数只用于上下文选择，不是根因置信度。
    """

    scores = {seed.node.node_id: seed.final_score for seed in result.seeds}
    for path in result.paths:
        for node in path.nodes:
            scores[node.node_id] = max(scores.get(node.node_id, 0.0), path.final_score)
    return scores


def _collect_node_candidates(
    result: GraphRetrievalResult,
    *,
    node_scores: dict[str, float],
) -> dict[str, BundledKnowledgeNode]:
    """从种子与路径收集唯一节点，并转换成不含 embedding 的紧凑证据对象。

    先遍历种子再遍历路径保持可重复插入顺序；相同 node_id 只构造一次，路径中的 ORM/领域副本不会
    覆盖已选择内容。每个候选使用 `_bundle_node` 生成稳定 `kn_*` 引用。
    """

    nodes: dict[str, KnowledgeNode] = {}
    for seed in result.seeds:
        nodes.setdefault(seed.node.node_id, seed.node)
    for path in result.paths:
        for node in path.nodes:
            nodes.setdefault(node.node_id, node)
    return {
        node_id: _bundle_node(node, retrieval_score=node_scores[node_id])
        for node_id, node in nodes.items()
    }


def _bundle_node(
    node: KnowledgeNode,
    *,
    retrieval_score: float,
) -> BundledKnowledgeNode:
    """把知识节点转换为 Planner 可引用的紧凑证据，并排除别名和向量派生字段。

    `kn_<node_id>` 与知识库主键稳定对应；source_span 保留原始依据，content 提供可读语义。embedding、
    Provider 元数据和 aliases 只服务检索，不应消耗 Prompt 预算或被模型当作额外事实。
    """

    return BundledKnowledgeNode(
        evidence_id=f"{KNOWLEDGE_EVIDENCE_ID_PREFIX}{node.node_id}",
        node_id=node.node_id,
        node_type=node.node_type,
        name=node.name,
        content=node.content,
        source_id=node.source_id,
        source_span=node.source_span,
        reliability=node.reliability,
        retrieval_score=retrieval_score,
    )


def _bundle_path(path: ScoredGraphPath) -> BundledGraphPath:
    """把完整 ScoredGraphPath 压缩为保序 ID、关系、来源跨度和分数，不复制节点正文。

    node_ids 和 edge_ids 保留方向与跳序，edge_source_spans 让 Auditor 可核对每条关系；path_id 同时
    作为 evidence_id，使 Planner 报告引用与数据库消融测试使用同一稳定标识。
    """

    return BundledGraphPath(
        evidence_id=path.path_id,
        path_id=path.path_id,
        seed_node_id=path.seed_node_id,
        node_ids=[node.node_id for node in path.nodes],
        edge_ids=[edge.edge_id for edge in path.edges],
        relation_types=[edge.relation_type for edge in path.edges],
        edge_source_spans=[edge.source_span for edge in path.edges],
        source_ids=path.source_ids,
        depth=path.depth,
        path_score=path.score,
        hybrid_score=path.hybrid_score,
    )


def _bundle_chunk(chunk: ScoredDocumentChunk) -> BundledDocumentChunk:
    """把已评分的文档切片压缩为可引用证据，并丢弃评分分量与向量派生字段。

    只保留 `final_score` 作为 `retrieval_score`：语义/全文/权威度分量是检索可解释性所需，注入
    Prompt 却只会让模型把内部排序数字当成事实强度。`dc_*` 同时作为 evidence_id，使报告脚注、
    Auditor 核对与文档表主键指向同一标识，重新导入语料后旧引用不会指向别的正文。
    """

    return BundledDocumentChunk(
        evidence_id=chunk.chunk.chunk_id,
        chunk_id=chunk.chunk.chunk_id,
        doc_id=chunk.document.doc_id,
        doc_type=chunk.document.doc_type,
        title=chunk.document.title,
        heading_path=chunk.chunk.heading_path,
        content=chunk.chunk.content,
        source_id=chunk.document.source_id,
        revision=chunk.document.revision,
        reliability=chunk.document.reliability,
        retrieval_score=chunk.final_score,
    )


def _payload_size(
    nodes: list[BundledKnowledgeNode],
    paths: list[BundledGraphPath],
    documents: list[BundledDocumentChunk],
) -> int:
    """返回 selected_nodes/selected_paths/selected_documents 规范 JSON 的精确 UTF-8 字节数。

    `sort_keys`、紧凑分隔符和 `ensure_ascii=False` 保证中文按真实 UTF-8 计费且跨平台结果一致；
    预算只覆盖将注入 Prompt 的主体，不包含 omitted 诊断元数据或 Pydantic 字段描述。三类证据合并
    计费而不是各自独立，因为模型上下文是一个共享资源，分开计费会让总量在最坏情况下翻三倍。
    """

    payload = {
        "selected_nodes": [node.model_dump(mode="json") for node in nodes],
        "selected_paths": [path.model_dump(mode="json") for path in paths],
        "selected_documents": [chunk.model_dump(mode="json") for chunk in documents],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(serialized.encode("utf-8"))
