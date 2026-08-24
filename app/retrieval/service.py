"""编排 embedding、全文/向量种子融合、cross-encoder 重排、显式图扩展和混合评分。

服务只协调确定性检索步骤，不生成自然语言答案：Embedding Provider 负责查询向量，PostgreSQL
仓储分别返回 lexical/vector 候选和关系路径，可选 Reranker 在有界候选集上做第二阶段联合打分，
本模块按节点 ID 去重并保留每个评分分量。这样 Provider、数据库实现和评分策略可以独立替换，
同时 Planner/Auditor 始终收到可追溯结果；重排失败只降级为一阶段排序，不让检索整体失败。
"""

from __future__ import annotations

from app.retrieval.embeddings import EmbeddingProvider, knowledge_node_text
from app.retrieval.models import (
    GraphPath,
    GraphRetrievalResult,
    HybridScoringWeights,
    HybridSeedMatch,
    KnowledgeRelationType,
    LexicalSeedMatch,
    RetrievalChannel,
    RetrievalMode,
    ScoredGraphPath,
    VectorSeedMatch,
)
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.reranker import MAX_RERANK_DOCUMENTS, RerankerError, RerankerProvider
from app.retrieval.scoring import blend_scores, bounded_score

MAX_SEED_CANDIDATES = 20


class GraphRetrievalService:
    """将可替换向量生成、双路召回、可选重排、融合排序与白名单图扩展组成 GraphRAG。

    仓储和 Provider 通过构造注入，服务因此不持有连接或模型 SDK；评分权重是不可变 Pydantic 配置。
    每次调用先生成一个查询向量，再顺序使用同一 AsyncSession 执行两路 SQL，避免并发复用会话；
    融合后的候选先经 cross-encoder 重排再截断为种子，只有最终种子参与一至两跳扩展，从而在控制
    数据库、重排成本和上下文预算的同时，让"召回多、精排少"的两阶段收益真实可测。
    """

    def __init__(
        self,
        repository: PostgresGraphRepository,
        embedding_provider: EmbeddingProvider,
        *,
        score_weights: HybridScoringWeights | None = None,
        reranker: RerankerProvider | None = None,
        rerank_candidate_multiplier: int = 3,
        rerank_blend_weight: float = 0.4,
    ) -> None:
        """注入图仓储、Embedding Provider、可选评分权重与可选 cross-encoder 重排配置。

        默认权重来自产品文档示例；显式注入允许评测不同配方而无需修改 SQL。`reranker=None` 表示
        只跑一阶段，此时 `final_score` 恒等于 `hybrid_score`，结果里的 `reranker_model` 也真实为空。
        候选倍数决定一阶段多召回多少条供精排挑选，倍数越大精排收益越高但费用也线性上升。
        构造器不执行 I/O，Provider ID 与维度会在查询及数据库过滤时使用，防止不同向量空间混合。
        """

        if not 1 <= rerank_candidate_multiplier <= 8:
            raise ValueError("rerank_candidate_multiplier must be between 1 and 8")
        if not 0 <= rerank_blend_weight <= 1:
            raise ValueError("rerank_blend_weight must be between 0 and 1")
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._score_weights = score_weights or HybridScoringWeights()
        self._reranker = reranker
        self._rerank_candidate_multiplier = rerank_candidate_multiplier
        self._rerank_blend_weight = rerank_blend_weight

    async def retrieve(
        self,
        query: str,
        *,
        seed_limit: int = 5,
        max_hops: int = 2,
        mode: RetrievalMode = RetrievalMode.HYBRID_GRAPH,
    ) -> GraphRetrievalResult:
        """按显式模式执行向量/全文召回、节点去重、可选重排、图扩展和最终路径评分。

        Provider 必须返回恰好一个固定维度向量；所有模式执行向量 top-k，只有 hybrid_graph 加入
        全文候选，只有 vector_only 跳过关系扩展。启用重排时先按倍数多召回候选，精排融合后再截断
        到 seed_limit，因此 `candidate_count` 与最终种子数共同构成"精排提升多少"的可核对分母。
        路径继承种子分量并加入边权乘积，同 path_id 仅保留最终分更高的版本。模式、重排模型和融合
        权重都进入输出契约，确保消融结果可重放而非依赖隐藏开关。
        """

        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= seed_limit <= MAX_SEED_CANDIDATES:
            raise ValueError(f"seed_limit must be between 1 and {MAX_SEED_CANDIDATES}")

        # Provider 使用批量接口以兼容远程模型；单查询必须严格返回一个固定维度向量。
        query_vectors = await self._embedding_provider.embed_texts([query])
        if len(query_vectors) != 1:
            raise ValueError("embedding provider must return exactly one query vector")
        query_embedding = query_vectors[0]
        if len(query_embedding) != self._embedding_provider.dimensions:
            raise ValueError("query embedding length does not match provider dimensions")

        candidate_limit = self._candidate_limit(seed_limit)
        # vector-only/vector-graph 故意关闭全文通道，隔离图结构相对于纯向量检索的真实增益。
        lexical_matches: list[LexicalSeedMatch] = []
        if mode is RetrievalMode.HYBRID_GRAPH:
            lexical_matches = await self._repository.search_lexical_seeds(
                query,
                limit=candidate_limit,
            )
        vector_matches = await self._repository.search_vector_seeds(
            query_embedding,
            provider_id=self._embedding_provider.provider_id,
            limit=candidate_limit,
        )
        candidates = merge_seed_matches(
            lexical_matches,
            vector_matches,
            weights=self._score_weights,
            limit=candidate_limit,
        )
        seeds, reranker_model = await self._rerank_candidates(query, candidates)
        seeds = seeds[:seed_limit]

        # SIMILAR_TO 只由已确认案例注册器写入；纳入白名单后，case 向量种子才能从任一方向扩展
        # 到相关先例，同时 pending/rejected 因没有图节点而无法借此进入上下文。
        allowed_relations = {
            KnowledgeRelationType.DEPENDS_ON,
            KnowledgeRelationType.CAUSED_BY,
            KnowledgeRelationType.MANIFESTS_AS,
            KnowledgeRelationType.RESOLVED_BY,
            KnowledgeRelationType.RUNS_ON,
            KnowledgeRelationType.PRODUCES,
            KnowledgeRelationType.CONSUMES,
            KnowledgeRelationType.SIMILAR_TO,
        }
        paths_by_id: dict[str, ScoredGraphPath] = {}
        if mode is not RetrievalMode.VECTOR_ONLY:
            for seed in seeds:
                paths = await self._repository.expand_paths(
                    seed.node.node_id,
                    max_hops=max_hops,
                    allowed_relations=allowed_relations,
                )
                for path in paths:
                    scored_path = score_graph_path(
                        path,
                        seed=seed,
                        weights=self._score_weights,
                        rerank_blend_weight=self._rerank_blend_weight,
                    )
                    current = paths_by_id.get(path.path_id)
                    if current is None or scored_path.final_score > current.final_score:
                        # 多种子命中同一路径时只保留解释分更强的一版，真实 edge 序列保持不变。
                        paths_by_id[path.path_id] = scored_path

        return GraphRetrievalResult(
            query=query,
            mode=mode,
            seed_limit=seed_limit,
            max_hops=max_hops,
            embedding_provider=self._embedding_provider.provider_id,
            reranker_model=reranker_model,
            candidate_count=len(candidates),
            score_weights=self._score_weights,
            rerank_blend_weight=self._rerank_blend_weight if reranker_model else 0,
            seeds=seeds,
            paths=sorted(
                paths_by_id.values(),
                key=lambda path: (-path.final_score, -path.depth, path.path_id),
            ),
        )

    def _candidate_limit(self, seed_limit: int) -> int:
        """计算一阶段召回条数：无重排时等于 seed_limit，有重排时按倍数放大并双重设限。

        放大是两阶段检索的收益来源——精排只能在候选集内部改名次，候选太少就无从提升。上限同时受
        仓储 top-k 契约和重排端点单次文档数约束，避免配置一个大倍数后请求被远程截断却无人察觉。
        """

        if self._reranker is None:
            return seed_limit
        multiplied = seed_limit * self._rerank_candidate_multiplier
        return max(seed_limit, min(multiplied, MAX_SEED_CANDIDATES, MAX_RERANK_DOCUMENTS))

    async def _rerank_candidates(
        self,
        query: str,
        candidates: list[HybridSeedMatch],
    ) -> tuple[list[HybridSeedMatch], str | None]:
        """对候选执行第二阶段联合打分并返回重排后的种子与实际使用的模型名。

        未配置重排器或候选为空时原样返回并把模型名留空，使报告不会把一阶段排序说成精排结果。
        `RerankerError` 一律降级为"保留一阶段排序"：重排是可选增强，把它变成可用性依赖会让一个
        计费外部服务抖动直接击穿整条诊断链路。分数与候选按输入顺序严格对齐后才重新排序。
        """

        if self._reranker is None or not candidates:
            return candidates, None

        documents = [knowledge_node_text(candidate.node) for candidate in candidates]
        try:
            scores = await self._reranker.rerank(query, documents)
        except RerankerError:
            return candidates, None
        if len(scores) != len(candidates):
            return candidates, None

        reranked = [
            candidate.model_copy(
                update={
                    "rerank_score": bounded_score(score),
                    "final_score": blend_scores(
                        candidate.hybrid_score,
                        bounded_score(score),
                        blend=self._rerank_blend_weight,
                    ),
                }
            )
            for candidate, score in zip(candidates, scores, strict=True)
        ]
        return (
            sorted(reranked, key=lambda match: (-match.final_score, match.node.node_id)),
            self._reranker.model,
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
    本函数只产出一阶段结果，因此 `final_score` 等于 `hybrid_score` 且 `rerank_score` 留空；只有
    检索服务在真正调用 cross-encoder 之后才会覆盖这两个字段，保证"是否精排过"永远可从数据判断。
    """

    if not 1 <= limit <= MAX_SEED_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_SEED_CANDIDATES}")

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
        lexical_score = bounded_score(lexical.lexical_score if lexical is not None else 0)
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
                hybrid_score=bounded_score(hybrid_score),
                final_score=bounded_score(hybrid_score),
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
    rerank_blend_weight: float = 0,
) -> ScoredGraphPath:
    """将种子分量与原始边权路径分相加，构造最终可审计 ScoredGraphPath。

    `path.score` 是关系边权乘积，乘以 path 权重后与语义、全文、可靠性和新鲜度共同组成一阶段分；
    种子若带 `rerank_score`，路径按同一融合权重继承它得到 `final_score`——路径的相关性来源是
    "这个种子值得展开"，因此重复把拼接文本送进 cross-encoder 只会增加成本而不增加信息。
    函数不改变节点、边、source_ids 或 path_id，因此分数调参不会伪造图结构引用。
    """

    hybrid_score = bounded_score(
        seed.semantic_score * weights.semantic
        + seed.lexical_score * weights.lexical
        + path.score * weights.path
        + seed.reliability_score * weights.reliability
        + seed.freshness_score * weights.freshness
    )
    final_score = hybrid_score
    if seed.rerank_score is not None:
        final_score = blend_scores(
            hybrid_score,
            seed.rerank_score,
            blend=rerank_blend_weight,
        )
    return ScoredGraphPath(
        **path.model_dump(),
        seed_node_id=seed.node.node_id,
        channels=seed.channels,
        semantic_score=seed.semantic_score,
        lexical_score=seed.lexical_score,
        reliability_score=seed.reliability_score,
        freshness_score=seed.freshness_score,
        hybrid_score=hybrid_score,
        rerank_score=seed.rerank_score,
        final_score=final_score,
    )
