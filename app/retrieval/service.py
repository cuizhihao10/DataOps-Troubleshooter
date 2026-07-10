"""编排 embedding、全文/向量种子融合、显式图扩展和五项混合评分。

服务只协调确定性检索步骤，不生成自然语言答案：Embedding Provider 负责查询向量，PostgreSQL
仓储分别返回 lexical/vector 候选和关系路径，本模块按节点 ID 去重并保留每个评分分量。这样
Provider、数据库实现和评分策略可以独立替换，同时 Planner/Auditor 始终收到可追溯结果。
"""

from __future__ import annotations

from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.models import (
    GraphPath,
    GraphRetrievalResult,
    HybridScoringWeights,
    HybridSeedMatch,
    KnowledgeRelationType,
    LexicalSeedMatch,
    RetrievalChannel,
    ScoredGraphPath,
    VectorSeedMatch,
)
from app.retrieval.repository import PostgresGraphRepository


class GraphRetrievalService:
    """将可替换向量生成、双路召回、融合排序与白名单图扩展组成 GraphRAG。

    仓储和 Provider 通过构造注入，服务因此不持有连接或模型 SDK；评分权重是不可变 Pydantic 配置。
    每次调用先生成一个查询向量，再顺序使用同一 AsyncSession 执行两路 SQL，避免并发复用会话；
    合并后的有限种子才参与一至两跳扩展，控制数据库和上下文成本。
    """

    def __init__(
        self,
        repository: PostgresGraphRepository,
        embedding_provider: EmbeddingProvider,
        *,
        score_weights: HybridScoringWeights | None = None,
    ) -> None:
        """注入图仓储、Embedding Provider 和可选五项评分权重。

        默认权重来自产品文档示例；显式注入允许评测不同配方而无需修改 SQL。构造器不执行 I/O，
        Provider ID 与维度会在查询及数据库过滤时使用，防止不同向量空间混合。
        """

        self._repository = repository
        self._embedding_provider = embedding_provider
        self._score_weights = score_weights or HybridScoringWeights()

    async def retrieve(
        self,
        query: str,
        *,
        seed_limit: int = 5,
        max_hops: int = 2,
    ) -> GraphRetrievalResult:
        """执行查询 embedding、双路 top-k、节点去重、图扩展和最终路径评分。

        Provider 必须返回恰好一个符合固定维度的向量；全文和向量查询各取 top-k，再由融合函数按
        hybrid_score 截断为总 seed_limit。每条路径继承种子的语义/全文/可靠性分量，并加入边权
        乘积形成的路径分量；同 path_id 只保留得分最高版本。无命中返回空列表，不伪造知识证据。
        """

        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= seed_limit <= 20:
            raise ValueError("seed_limit must be between 1 and 20")

        # Provider 使用批量接口以兼容远程模型；单查询必须严格返回一个固定维度向量。
        query_vectors = await self._embedding_provider.embed_texts([query])
        if len(query_vectors) != 1:
            raise ValueError("embedding provider must return exactly one query vector")
        query_embedding = query_vectors[0]
        if len(query_embedding) != self._embedding_provider.dimensions:
            raise ValueError("query embedding length does not match provider dimensions")

        # 同一个 AsyncSession 不并发执行 SQL；顺序查询仍保持两路召回边界和独立原始分数。
        lexical_matches = await self._repository.search_lexical_seeds(query, limit=seed_limit)
        vector_matches = await self._repository.search_vector_seeds(
            query_embedding,
            provider_id=self._embedding_provider.provider_id,
            limit=seed_limit,
        )
        seeds = merge_seed_matches(
            lexical_matches,
            vector_matches,
            weights=self._score_weights,
            limit=seed_limit,
        )

        # SIMILAR_TO 留给已确认案例能力；当前在线故障链只沿事实、因果和方案关系扩展。
        allowed_relations = {
            KnowledgeRelationType.DEPENDS_ON,
            KnowledgeRelationType.CAUSED_BY,
            KnowledgeRelationType.MANIFESTS_AS,
            KnowledgeRelationType.RESOLVED_BY,
            KnowledgeRelationType.RUNS_ON,
            KnowledgeRelationType.PRODUCES,
            KnowledgeRelationType.CONSUMES,
        }
        paths_by_id: dict[str, ScoredGraphPath] = {}
        for seed in seeds:
            paths = await self._repository.expand_paths(
                seed.node.node_id,
                max_hops=max_hops,
                allowed_relations=allowed_relations,
            )
            for path in paths:
                scored_path = score_graph_path(path, seed=seed, weights=self._score_weights)
                current = paths_by_id.get(path.path_id)
                if current is None or scored_path.hybrid_score > current.hybrid_score:
                    # 多个种子到达同一路径时保留解释分量更强的一版，path_id 仍由真实边序列决定。
                    paths_by_id[path.path_id] = scored_path

        return GraphRetrievalResult(
            query=query,
            embedding_provider=self._embedding_provider.provider_id,
            score_weights=self._score_weights,
            seeds=seeds,
            paths=sorted(
                paths_by_id.values(),
                key=lambda path: (-path.hybrid_score, -path.depth, path.path_id),
            ),
        )


def merge_seed_matches(
    lexical_matches: list[LexicalSeedMatch],
    vector_matches: list[VectorSeedMatch],
    *,
    weights: HybridScoringWeights,
    limit: int,
) -> list[HybridSeedMatch]:
    """按 node_id 合并两路候选，裁剪原始分数并计算可解释种子混合分。

    全文 ts_rank/bonus 可能超过一，因此先裁剪到评分契约范围；向量分数已由仓储标准化。相同节点
    命中两路时合并通道并保留两项分数，单路命中时另一项为零。种子阶段没有路径分量，案例新鲜度
    也尚未接入，因此 hybrid_score 只累加当前适用项，但仍使用完整全局权重以便与路径分数衔接。
    """

    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")

    lexical_by_id = {match.node.node_id: match for match in lexical_matches}
    vector_by_id = {match.node.node_id: match for match in vector_matches}
    node_ids = lexical_by_id.keys() | vector_by_id.keys()
    merged: list[HybridSeedMatch] = []
    for node_id in node_ids:
        lexical = lexical_by_id.get(node_id)
        vector = vector_by_id.get(node_id)
        if vector is not None:
            node = vector.node
        elif lexical is not None:
            node = lexical.node
        else:  # pragma: no cover - node_ids is the union of the two dictionaries.
            raise RuntimeError("merged seed ID is absent from both retrieval channels")
        lexical_score = _bounded_score(lexical.lexical_score if lexical is not None else 0)
        semantic_score = vector.semantic_score if vector is not None else 0
        channels = []
        if lexical is not None:
            channels.append(RetrievalChannel.LEXICAL)
        if vector is not None:
            channels.append(RetrievalChannel.VECTOR)

        # 可靠性来自人工知识节点；freshness 等案例时间字段进入模型后可在同一公式中补齐。
        reliability_score = node.reliability
        freshness_score = 0.0
        hybrid_score = (
            semantic_score * weights.semantic
            + lexical_score * weights.lexical
            + reliability_score * weights.reliability
            + freshness_score * weights.freshness
        )
        merged.append(
            HybridSeedMatch(
                node=node,
                channels=channels,
                semantic_score=semantic_score,
                lexical_score=lexical_score,
                reliability_score=reliability_score,
                freshness_score=freshness_score,
                hybrid_score=_bounded_score(hybrid_score),
            )
        )

    return sorted(
        merged,
        key=lambda match: (-match.hybrid_score, match.node.node_id),
    )[:limit]


def score_graph_path(
    path: GraphPath,
    *,
    seed: HybridSeedMatch,
    weights: HybridScoringWeights,
) -> ScoredGraphPath:
    """将种子分量与原始边权路径分相加，构造最终可审计 ScoredGraphPath。

    `path.score` 是关系边权乘积，乘以 path 权重后与语义、全文、可靠性和新鲜度共同组成最终分；
    函数不改变节点、边、source_ids 或 path_id，因此分数调参不会伪造图结构引用。
    """

    hybrid_score = (
        seed.semantic_score * weights.semantic
        + seed.lexical_score * weights.lexical
        + path.score * weights.path
        + seed.reliability_score * weights.reliability
        + seed.freshness_score * weights.freshness
    )
    return ScoredGraphPath(
        **path.model_dump(),
        seed_node_id=seed.node.node_id,
        channels=seed.channels,
        semantic_score=seed.semantic_score,
        lexical_score=seed.lexical_score,
        reliability_score=seed.reliability_score,
        freshness_score=seed.freshness_score,
        hybrid_score=_bounded_score(hybrid_score),
    )


def _bounded_score(value: float) -> float:
    """把数据库或浮点组合分裁剪到统一零到一范围，避免边界误差破坏 Schema。

    PostgreSQL ts_rank 没有固定上界，cosine 和小数加权也可能出现极小浮点越界；集中裁剪使所有
    对外分数遵守契约。该函数不做归一化或重新排序，因此不会掩盖配置权重错误。
    """

    return max(0.0, min(1.0, float(value)))
