"""编排文档 RAG：查询向量、双通道切片召回、可选 cross-encoder 重排与三因子混合评分。

结构与 GraphRAG 服务刻意保持一致（先召回、再精排、失败只降级），但评分因子不同：切片没有关系边
也没有可信的时间戳，因此只用语义、全文和文档权威度三项，硬塞 path/freshness 只会让公式看起来
复杂而不更准确。`blend_scores` 与 `bounded_score` 与图侧共享同一实现而非各自复制，保证两条通道
的融合口径完全相同——否则"重排带来多少提升"在两处会得到不可比较的数字。

服务不生成任何自然语言结论：空召回是合法结果，调用方必须据此声明不确定性而不是编造处置步骤。
"""

from __future__ import annotations

from app.retrieval.document_repository import PostgresDocumentRepository
from app.retrieval.documents import (
    DocumentRetrievalResult,
    DocumentScoringWeights,
    LexicalChunkMatch,
    ScoredDocumentChunk,
    VectorChunkMatch,
    document_chunk_text,
)
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.reranker import MAX_RERANK_DOCUMENTS, RerankerError, RerankerProvider
from app.retrieval.scoring import RetrievalChannel, blend_scores, bounded_score

MAX_CHUNK_CANDIDATES = 40


class DocumentRetrievalService:
    """把可替换向量生成、全文/向量切片召回、可选重排与三因子融合组成文档 RAG。

    仓储与 Provider 由构造注入，因此服务不持有连接或模型 SDK；两路 SQL 顺序执行以免并发复用同一
    AsyncSession。启用重排时先按倍数多召回候选再精排截断，`candidate_count` 因此是"精排提升多少"
    的可核对分母；重排失败一律降级为一阶段排序，绝不让一个计费外部服务成为诊断链路的可用性依赖。
    """

    def __init__(
        self,
        repository: PostgresDocumentRepository,
        embedding_provider: EmbeddingProvider,
        *,
        score_weights: DocumentScoringWeights | None = None,
        reranker: RerankerProvider | None = None,
        rerank_candidate_multiplier: int = 3,
        rerank_blend_weight: float = 0.4,
    ) -> None:
        """注入文档仓储、Embedding Provider、可选评分权重与可选 cross-encoder 重排配置。

        默认与 GraphRAG 共享同一个 Provider 实例和同一组重排参数，使两条通道的向量空间和融合口径
        天然一致；`reranker=None` 表示只跑一阶段，此时结果里的 `reranker_model` 真实为空而不是伪装。
        构造器不执行任何 I/O，非法倍数或融合权重在此立即失败，避免错误配置留到首次检索才暴露。
        """

        if not 1 <= rerank_candidate_multiplier <= 8:
            raise ValueError("rerank_candidate_multiplier must be between 1 and 8")
        if not 0 <= rerank_blend_weight <= 1:
            raise ValueError("rerank_blend_weight must be between 0 and 1")
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._score_weights = score_weights or DocumentScoringWeights()
        self._reranker = reranker
        self._rerank_candidate_multiplier = rerank_candidate_multiplier
        self._rerank_blend_weight = rerank_blend_weight

    async def retrieve(
        self,
        query: str,
        *,
        chunk_limit: int = 4,
    ) -> DocumentRetrievalResult:
        """执行双通道切片召回、按 chunk_id 去重合并、可选重排，返回有界文档证据。

        Provider 必须返回恰好一个固定维度向量，否则说明配置维度与真实模型不一致，此时显式失败远比
        用一个错长度向量去查库更安全。重排模型名与融合权重都进入输出契约，使消融结果可以重放。
        """

        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= chunk_limit <= 20:
            raise ValueError("chunk_limit must be between 1 and 20")

        query_vectors = await self._embedding_provider.embed_texts([query])
        if len(query_vectors) != 1:
            raise ValueError("embedding provider must return exactly one query vector")
        query_embedding = query_vectors[0]
        if len(query_embedding) != self._embedding_provider.dimensions:
            raise ValueError("query embedding length does not match provider dimensions")

        candidate_limit = self._candidate_limit(chunk_limit)
        lexical_matches = await self._repository.search_lexical_chunks(
            query,
            limit=candidate_limit,
        )
        vector_matches = await self._repository.search_vector_chunks(
            query_embedding,
            provider_id=self._embedding_provider.provider_id,
            limit=candidate_limit,
        )
        candidates = merge_chunk_matches(
            lexical_matches,
            vector_matches,
            weights=self._score_weights,
            limit=candidate_limit,
        )
        chunks, reranker_model = await self._rerank_candidates(query, candidates)

        return DocumentRetrievalResult(
            query=query,
            chunk_limit=chunk_limit,
            embedding_provider=self._embedding_provider.provider_id,
            reranker_model=reranker_model,
            candidate_count=len(candidates),
            score_weights=self._score_weights,
            rerank_blend_weight=self._rerank_blend_weight if reranker_model else 0,
            chunks=chunks[:chunk_limit],
        )

    def _candidate_limit(self, chunk_limit: int) -> int:
        """计算一阶段召回条数：无重排时等于 chunk_limit，有重排时按倍数放大并双重设限。

        放大是两阶段检索唯一的收益来源——精排只能在候选集内部改名次。上限同时受仓储 top-k 契约与
        重排端点单次文档数约束，避免配置一个大倍数后请求被远程静默截断却无人察觉。
        """

        if self._reranker is None:
            return chunk_limit
        multiplied = chunk_limit * self._rerank_candidate_multiplier
        return max(chunk_limit, min(multiplied, MAX_CHUNK_CANDIDATES, MAX_RERANK_DOCUMENTS))

    async def _rerank_candidates(
        self,
        query: str,
        candidates: list[ScoredDocumentChunk],
    ) -> tuple[list[ScoredDocumentChunk], str | None]:
        """对候选切片执行第二阶段联合打分，返回重排结果与实际使用的模型名。

        未配置重排器、候选为空或分数条数不齐时都原样返回并把模型名留空，使报告不会把一阶段排序说成
        精排结果；`RerankerError` 同样降级而不抛出，因为文档证据缺失只会降低结论质量，不该中断诊断。
        """

        if self._reranker is None or not candidates:
            return candidates, None

        documents = [
            document_chunk_text(candidate.document, candidate.chunk) for candidate in candidates
        ]
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
            sorted(reranked, key=lambda match: (-match.final_score, match.chunk.chunk_id)),
            self._reranker.model,
        )


def merge_chunk_matches(
    lexical_matches: list[LexicalChunkMatch],
    vector_matches: list[VectorChunkMatch],
    *,
    weights: DocumentScoringWeights,
    limit: int,
) -> list[ScoredDocumentChunk]:
    """按 chunk_id 合并两路候选，裁剪原始分数并计算三因子可解释混合分。

    全文 ts_rank 与 LIKE bonus 之和可能超过一，因此先裁剪到评分契约范围；向量分数已由仓储标准化。
    同一切片命中两路时合并通道并保留两项分数，单路命中时另一项为零，从而"为什么这段被选中"始终
    可以逐项复核。本函数只产出一阶段结果，`final_score` 等于 `hybrid_score` 且 `rerank_score` 留空。
    """

    if not 1 <= limit <= MAX_CHUNK_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CHUNK_CANDIDATES}")

    lexical_by_id = {match.chunk.chunk_id: match for match in lexical_matches}
    vector_by_id = {match.chunk.chunk_id: match for match in vector_matches}
    merged: list[ScoredDocumentChunk] = []
    for chunk_id in lexical_by_id.keys() | vector_by_id.keys():
        lexical = lexical_by_id.get(chunk_id)
        vector = vector_by_id.get(chunk_id)
        # 两路都命中时优先采用向量分支的对象：它与查询在同一 Provider 空间，元数据来源也更完整。
        source = vector if vector is not None else lexical
        if source is None:  # pragma: no cover - 键集合是两个字典的并集，不可能同时缺失。
            raise RuntimeError("merged chunk ID is absent from both retrieval channels")

        lexical_score = bounded_score(lexical.lexical_score if lexical is not None else 0)
        semantic_score = vector.semantic_score if vector is not None else 0.0
        channels: list[RetrievalChannel] = []
        if lexical is not None:
            channels.append(RetrievalChannel.LEXICAL)
        if vector is not None:
            channels.append(RetrievalChannel.VECTOR)

        # authority 直接取文档声明可靠性：文档域没有边权也没有可信时间戳，人工评审是唯一诚实依据。
        authority_score = source.document.reliability
        hybrid_score = bounded_score(
            semantic_score * weights.semantic
            + lexical_score * weights.lexical
            + authority_score * weights.authority
        )
        merged.append(
            ScoredDocumentChunk(
                document=source.document,
                chunk=source.chunk,
                channels=channels,
                semantic_score=semantic_score,
                lexical_score=lexical_score,
                authority_score=authority_score,
                hybrid_score=hybrid_score,
                final_score=hybrid_score,
            )
        )

    return sorted(
        merged,
        key=lambda match: (-match.hybrid_score, match.chunk.chunk_id),
    )[:limit]
