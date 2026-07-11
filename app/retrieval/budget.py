"""将完整 GraphRAG 结果裁剪成受 UTF-8 JSON、节点数和路径数约束的证据 Bundle。

预算选择是确定性基础设施，不交给 LLM 自行摘要或删除证据。算法优先按检索排序原子加入完整路径
及其所有节点，再补充未出现的高分种子；任何候选如果会突破任一预算就整体省略并记录稳定 ID，
从而保证 Planner 看不到断裂路径，也能知道上下文因预算而不完整。
"""

from __future__ import annotations

import json

from app.retrieval.models import (
    BundledGraphPath,
    BundledKnowledgeNode,
    EvidenceBundleBudget,
    GraphEvidenceBundle,
    GraphRetrievalResult,
    KnowledgeNode,
    ScoredGraphPath,
)


def build_evidence_bundle(
    result: GraphRetrievalResult,
    *,
    budget: EvidenceBundleBudget,
) -> GraphEvidenceBundle:
    """按完整路径优先策略构造从不超过三重预算的 GraphEvidenceBundle。

    每条路径与尚未选择的路径节点作为一个原子候选，只有节点数、路径数和规范 JSON 字节数都满足
    才纳入；随后按种子混合分补充独立节点。所有候选 ID 最终分成 selected 或 omitted，两边不重叠。
    空检索结果合法返回只含规范空列表包装的最小主体，输入结果本身不会被修改。
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
        if _payload_size(proposed_nodes, proposed_paths) > budget.max_bytes:
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
        if _payload_size(proposed_nodes, selected_paths) > budget.max_bytes:
            continue
        selected_nodes[seed.node.node_id] = candidate

    selected_node_ids = set(selected_nodes)
    selected_path_ids = {path.path_id for path in selected_paths}
    all_node_ids = set(node_candidates)
    all_path_ids = {path.path_id for path in result.paths}
    omitted_node_ids = sorted(all_node_ids - selected_node_ids)
    omitted_path_ids = sorted(all_path_ids - selected_path_ids)
    used_bytes = _payload_size(list(selected_nodes.values()), selected_paths)

    return GraphEvidenceBundle(
        query=result.query,
        retrieval_mode=result.mode,
        budget=budget,
        used_bytes=used_bytes,
        selected_nodes=list(selected_nodes.values()),
        selected_paths=selected_paths,
        omitted_node_ids=omitted_node_ids,
        omitted_path_ids=omitted_path_ids,
        truncated=bool(omitted_node_ids or omitted_path_ids),
    )


def _collect_node_scores(result: GraphRetrievalResult) -> dict[str, float]:
    """为所有种子和路径节点计算它们在本次检索中的最高可解释优先分。

    种子使用自身 hybrid_score，非种子路径节点继承包含它的最高路径分；同一节点出现多次时取最大值，
    既保证稳定排序信息，又不把多次出现机械累加成更强事实。该分数只用于上下文选择，不是根因置信度。
    """

    scores = {seed.node.node_id: seed.hybrid_score for seed in result.seeds}
    for path in result.paths:
        for node in path.nodes:
            scores[node.node_id] = max(scores.get(node.node_id, 0.0), path.hybrid_score)
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
        evidence_id=f"kn_{node.node_id}",
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


def _payload_size(
    nodes: list[BundledKnowledgeNode],
    paths: list[BundledGraphPath],
) -> int:
    """返回 selected_nodes/selected_paths 规范 JSON 的精确 UTF-8 字节数。

    `sort_keys`、紧凑分隔符和 `ensure_ascii=False` 保证中文按真实 UTF-8 计费且跨平台结果一致；
    预算只覆盖将注入 Prompt 的主体，不包含 omitted 诊断元数据或 Pydantic 字段描述。
    """

    payload = {
        "selected_nodes": [node.model_dump(mode="json") for node in nodes],
        "selected_paths": [path.model_dump(mode="json") for path in paths],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(serialized.encode("utf-8"))
