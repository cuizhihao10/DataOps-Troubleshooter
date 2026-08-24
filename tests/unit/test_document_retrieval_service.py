"""验证文档 RAG 服务的双通道合并、三因子评分、两阶段重排与失败降级语义。

文档检索最终决定哪几条处置步骤进入报告，因此这里锁定的都是"不会报错但会改变结论"的行为：同一
切片被两路命中时必须合并通道并保留两项分数；重排必须只在候选集内部改名次并留下 `rerank_score`
解释；重排失败、分数条数不齐或未配置重排器时，结果里的 `reranker_model` 必须真实为空，绝不能把
一阶段排序说成精排结果。测试用替身仓储与替身 Provider，因此不触网也不依赖数据库。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.retrieval.document_service import (
    MAX_CHUNK_CANDIDATES,
    DocumentRetrievalService,
    merge_chunk_matches,
)
from app.retrieval.documents import (
    DocumentChunk,
    DocumentMetadata,
    DocumentScoringWeights,
    DocumentType,
    LexicalChunkMatch,
    VectorChunkMatch,
    make_chunk_id,
)
from app.retrieval.reranker import RerankerError
from app.retrieval.scoring import RetrievalChannel


def _document(doc_id: str, *, reliability: float = 0.9) -> DocumentMetadata:
    """构造一份文档元数据，可靠性可覆盖以便断言 authority 因子确实来自声明值。

    doc_id 作为参数是因为多数用例需要两份不同来源的文档来验证权威度差异；其余字段固定，让分数
    断言只受被测变量影响。
    """

    return DocumentMetadata(
        doc_id=doc_id,
        doc_type=DocumentType.RUNBOOK,
        title=f"{doc_id} 手册",
        components=["FlashSync"],
        source_id=f"synthetic-{doc_id}",
        revision="v1.0",
        reliability=reliability,
    )


def _chunk(doc_id: str, ordinal: int = 0) -> DocumentChunk:
    """构造一个合法切片，正文与序号绑定以便在断言里区分不同片段。

    引用 ID 由 `make_chunk_id` 生成而不是手写，保证测试使用与生产完全一致的 `dc_*` 规则，也顺带
    覆盖"同文档不同序号必须得到不同引用"这一前提。
    """

    content = f"{doc_id} 第 {ordinal} 段处置步骤。"
    return DocumentChunk(
        chunk_id=make_chunk_id(doc_id, ordinal),
        doc_id=doc_id,
        ordinal=ordinal,
        heading_path=f"{doc_id} 手册 > 处置步骤",
        content=content,
        char_count=len(content),
    )


class _StubRepository:
    """按预设结果回答两路切片召回，并记录收到的 limit 供候选放大断言使用。

    只实现服务真正调用的两个方法，因此测试不需要数据库或 AsyncSession；记录 limit 是为了证明
    启用重排后一阶段确实多召回了候选——否则两阶段检索没有任何收益来源。
    """

    def __init__(
        self,
        lexical: list[LexicalChunkMatch],
        vector: list[VectorChunkMatch],
    ) -> None:
        """保存两路预设候选并初始化 limit 记录。

        预设列表原样返回而不做截断，使"服务是否按 chunk_limit 正确截断结果"成为可被观察的行为，
        而不是被替身仓储提前掩盖。
        """

        self._lexical = lexical
        self._vector = vector
        self.requested_limits: list[int] = []

    async def search_lexical_chunks(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[LexicalChunkMatch]:
        """返回预设的全文候选并记录本次请求的候选上限。

        签名与真实仓储保持一致（关键字 limit），因此把替身换成真实实现时服务代码无需改动；记录下来的
        上限则让"启用重排后一阶段是否真的多召回"成为可直接断言的事实，而不是从最终条数间接推测。
        """

        self.requested_limits.append(limit)
        return list(self._lexical)

    async def search_vector_chunks(
        self,
        embedding: Sequence[float],
        *,
        provider_id: str,
        limit: int,
    ) -> list[VectorChunkMatch]:
        """返回预设的向量候选并记录本次请求的候选上限。

        忽略传入向量与 provider_id 的具体取值，但保留形参，使服务侧漏传 provider 过滤条件时会立刻
        因签名不匹配而失败，而不是静默跨向量空间比较。
        """

        self.requested_limits.append(limit)
        return list(self._vector)


class _StubEmbeddingProvider:
    """返回固定维度常量向量的 Embedding 替身，可注入错误长度以验证显式失败。

    文档服务只要求"恰好一个固定维度向量"，用常量向量即可满足；维度可覆盖，用来复现"配置维度与
    真实模型不一致"这种最容易被忽略的错误配置。
    """

    provider_id = "deterministic-hash:v1"

    def __init__(self, dimensions: int = 8, *, emitted_dimensions: int | None = None) -> None:
        """声明对外维度，并可单独指定实际产出的向量长度以制造不一致。

        两个维度分开保存，是因为真实故障形态正是"Provider 宣称 1024 维但返回了别的长度"，把它们
        合成一个字段就无法在单元测试里复现。
        """

        self.dimensions = dimensions
        self._emitted_dimensions = emitted_dimensions or dimensions

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """为每个输入文本返回一个非零常量向量，长度取实际产出维度。

        常量向量不影响任何断言，因为向量召回结果由替身仓储直接给出；这里只需满足服务对"恰好一条、
        长度等于声明维度、且不是全零"这三项前置校验的要求，让被测行为集中在合并与重排逻辑上。
        """

        return [[0.5] * self._emitted_dimensions for _ in texts]


class _StubReranker:
    """按 chunk 顺序返回预设分数的重排替身，也可抛出 `RerankerError` 触发降级。

    分数按候选顺序而不是按 ID 给出，因此测试可以精确构造"二阶段把末位候选提到首位"这种只有真实
    重排才会产生的名次变化；异常分支用来证明计费外部服务不是诊断链路的可用性依赖。
    """

    provider_id = "bge-reranker-v2-m3:v1"
    model = "BAAI/bge-reranker-v2-m3"

    def __init__(self, scores: list[float] | None = None, *, error: bool = False) -> None:
        """保存预设分数或失败开关，并记录实际收到的候选数量。

        记录候选数量让"重排前多召回、重排后截断"这条两阶段纪律可以被直接断言，而不是从最终条数
        间接推断；失败开关独立于分数，使"外部服务不可用"与"分数条数不齐"两类降级能分开覆盖。
        """

        self._scores = scores or []
        self._error = error
        self.received_documents: list[str] = []

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """记录候选文本后返回预设分数，或按开关抛出领域异常。

        返回列表长度可以刻意与候选数量不一致，用于验证服务在分数条数不齐时整体降级，而不是把
        分数错配到别的切片上。
        """

        self.received_documents = list(documents)
        if self._error:
            raise RerankerError("stub reranker failure")
        return list(self._scores)


def test_merge_combines_channels_and_keeps_both_component_scores() -> None:
    """验证同一切片被两路命中时合并通道，单路命中时另一项分数为零。

    "为什么这段被选中"必须能逐项复核：合并后若只保留一路分数，一个纯关键词命中的片段会看起来
    像是语义高度相关。断言同时检查混合分等于三因子加权的精确值，防止实现悄悄改动权重口径。
    """

    document = _document("runbook_a", reliability=0.8)
    both = _chunk("runbook_a", 0)
    lexical_only = _chunk("runbook_a", 1)
    vector_only = _chunk("runbook_a", 2)
    weights = DocumentScoringWeights()

    merged = merge_chunk_matches(
        [
            LexicalChunkMatch(document=document, chunk=both, lexical_score=0.4),
            LexicalChunkMatch(document=document, chunk=lexical_only, lexical_score=0.6),
        ],
        [
            VectorChunkMatch(
                document=document,
                chunk=both,
                embedding_provider="deterministic-hash:v1",
                embedding_dimensions=8,
                semantic_score=0.9,
            ),
            VectorChunkMatch(
                document=document,
                chunk=vector_only,
                embedding_provider="deterministic-hash:v1",
                embedding_dimensions=8,
                semantic_score=0.5,
            ),
        ],
        weights=weights,
        limit=5,
    )

    by_id = {match.chunk.chunk_id: match for match in merged}
    assert by_id[both.chunk_id].channels == [RetrievalChannel.LEXICAL, RetrievalChannel.VECTOR]
    assert by_id[lexical_only.chunk_id].channels == [RetrievalChannel.LEXICAL]
    assert by_id[vector_only.chunk_id].channels == [RetrievalChannel.VECTOR]
    assert by_id[lexical_only.chunk_id].semantic_score == 0
    assert by_id[vector_only.chunk_id].lexical_score == 0
    assert by_id[both.chunk_id].authority_score == 0.8
    assert by_id[both.chunk_id].hybrid_score == pytest.approx(
        0.9 * weights.semantic + 0.4 * weights.lexical + 0.8 * weights.authority
    )
    # 一阶段结果必须原样暴露"未重排"：final 等于 hybrid 且 rerank_score 为空。
    assert all(match.rerank_score is None for match in merged)
    assert all(match.final_score == match.hybrid_score for match in merged)


def test_merge_sorts_by_hybrid_score_then_chunk_id_and_applies_limit() -> None:
    """验证合并结果按混合分降序、同分按 chunk_id 升序排列，并截断到给定条数。

    同分回退到 ID 是为了让排序完全确定：字典键集合的迭代顺序不稳定，若没有次级键，同分候选进入
    上下文的顺序会在不同进程间漂移，Golden Case 回放随之失去意义。
    """

    high = _document("runbook_high", reliability=1.0)
    low = _document("runbook_low", reliability=0.2)
    tied_chunks = [_chunk("runbook_low", 0), _chunk("runbook_low", 1)]
    weights = DocumentScoringWeights()

    def _vector(document: DocumentMetadata, chunk: DocumentChunk) -> VectorChunkMatch:
        """把文档与切片包装成固定语义分的向量候选，让排序只受 authority 与 ID 影响。

        全部候选共用同一个语义分是本测试成立的前提：只有这样才能证明权威度真的参与排序，以及
        分数完全相同时排序回退到稳定的 chunk_id。
        """

        return VectorChunkMatch(
            document=document,
            chunk=chunk,
            embedding_provider="deterministic-hash:v1",
            embedding_dimensions=8,
            semantic_score=0.5,
        )

    merged = merge_chunk_matches(
        [],
        [
            _vector(low, tied_chunks[1]),
            _vector(low, tied_chunks[0]),
            _vector(high, _chunk("runbook_high", 0)),
        ],
        weights=weights,
        limit=3,
    )

    # 语义分相同，authority 更高的文档必须胜出，证明权威度真的参与了排序而不是只被记录。
    assert merged[0].document.doc_id == "runbook_high"
    assert [match.chunk.chunk_id for match in merged[1:]] == sorted(
        chunk.chunk_id for chunk in tied_chunks
    )

    limited = merge_chunk_matches(
        [],
        [_vector(high, _chunk("runbook_high", 0)), _vector(low, tied_chunks[0])],
        weights=weights,
        limit=1,
    )
    assert [match.document.doc_id for match in limited] == ["runbook_high"]
    with pytest.raises(ValueError, match="limit must be between"):
        merge_chunk_matches([], [], weights=weights, limit=MAX_CHUNK_CANDIDATES + 1)


@pytest.mark.asyncio
async def test_disabled_reranker_reports_empty_model_and_no_blend_weight() -> None:
    """验证未配置重排器时只跑一阶段，`reranker_model` 为空且融合权重报告为零。

    这是"不把一阶段排序说成精排"的核心断言：只要模型名为空，报告和评测就能确定名次未经二阶段
    改写。候选上限同时等于 chunk_limit，证明没有为不存在的重排白多召回一批候选。
    """

    document = _document("runbook_a")
    repository = _StubRepository(
        [LexicalChunkMatch(document=document, chunk=_chunk("runbook_a", 0), lexical_score=0.5)],
        [],
    )
    service = DocumentRetrievalService(repository, _StubEmbeddingProvider())  # type: ignore[arg-type]

    result = await service.retrieve("同步任务主键冲突", chunk_limit=2)

    assert result.contract_id == "document-retrieval:v1"
    assert result.reranker_model is None
    assert result.rerank_blend_weight == 0
    assert result.candidate_count == 1
    assert result.embedding_provider == "deterministic-hash:v1"
    assert repository.requested_limits == [2, 2]
    assert [chunk.rerank_score for chunk in result.chunks] == [None]


@pytest.mark.asyncio
async def test_reranker_overfetches_candidates_and_blends_scores_into_new_order() -> None:
    """验证启用重排后按倍数多召回候选，二阶段分数经显式融合改变名次并被记录。

    多召回是两阶段检索唯一的收益来源：精排只能在候选集内部改名次。用例让一阶段末位候选拿到最高
    重排分，因此只有真正执行了融合与重排序才能通过；`candidate_count` 也必须保留精排前的分母。
    """

    document = _document("runbook_a", reliability=0.5)
    chunks = [_chunk("runbook_a", ordinal) for ordinal in range(3)]
    repository = _StubRepository(
        [
            LexicalChunkMatch(document=document, chunk=chunk, lexical_score=score)
            for chunk, score in zip(chunks, [0.9, 0.5, 0.1], strict=True)
        ],
        [],
    )
    reranker = _StubReranker([0.1, 0.2, 1.0])
    service = DocumentRetrievalService(
        repository,  # type: ignore[arg-type]
        _StubEmbeddingProvider(),
        reranker=reranker,  # type: ignore[arg-type]
        rerank_candidate_multiplier=3,
        rerank_blend_weight=0.4,
    )

    result = await service.retrieve("主键冲突", chunk_limit=1)

    assert repository.requested_limits == [3, 3]
    assert len(reranker.received_documents) == 3
    assert result.reranker_model == "BAAI/bge-reranker-v2-m3"
    assert result.rerank_blend_weight == 0.4
    assert result.candidate_count == 3
    assert len(result.chunks) == 1
    winner = result.chunks[0]
    assert winner.chunk.chunk_id == chunks[2].chunk_id
    assert winner.rerank_score == 1.0
    assert winner.final_score == pytest.approx(0.6 * winner.hybrid_score + 0.4)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reranker",
    [_StubReranker(error=True), _StubReranker([0.9])],
    ids=["provider-failure", "score-count-mismatch"],
)
async def test_rerank_failures_degrade_to_stage_one_without_claiming_rerank(
    reranker: _StubReranker,
) -> None:
    """验证重排失败或分数条数不齐时降级为一阶段排序，并把模型名留空。

    文档证据缺失只会降低结论质量，不该中断诊断，所以一个计费外部服务不能成为可用性依赖；条数
    不齐单列一个用例，因为部分分数会被错配到别的切片上，产生看似合理却完全错误的排序。
    """

    document = _document("runbook_a")
    chunks = [_chunk("runbook_a", ordinal) for ordinal in range(2)]
    repository = _StubRepository(
        [
            LexicalChunkMatch(document=document, chunk=chunk, lexical_score=score)
            for chunk, score in zip(chunks, [0.8, 0.2], strict=True)
        ],
        [],
    )
    service = DocumentRetrievalService(
        repository,  # type: ignore[arg-type]
        _StubEmbeddingProvider(),
        reranker=reranker,  # type: ignore[arg-type]
    )

    result = await service.retrieve("主键冲突", chunk_limit=2)

    assert result.reranker_model is None
    assert result.rerank_blend_weight == 0
    assert [chunk.chunk.chunk_id for chunk in result.chunks] == [
        chunks[0].chunk_id,
        chunks[1].chunk_id,
    ]
    assert all(chunk.final_score == chunk.hybrid_score for chunk in result.chunks)


@pytest.mark.asyncio
async def test_empty_recall_is_a_valid_result_rather_than_an_error() -> None:
    """验证两路都没有命中时返回空 chunks 的合法结果，而不是抛错或编造片段。

    空召回必须能传达到调用方，让报告声明不确定性；若这里改成抛错，一次"知识库确实没有这份文档"
    的正常情况会中断整条诊断链路，反而促使上层用兜底文本掩盖缺证。
    """

    service = DocumentRetrievalService(
        _StubRepository([], []),  # type: ignore[arg-type]
        _StubEmbeddingProvider(),
    )

    result = await service.retrieve("完全没有语料的查询")

    assert result.chunks == []
    assert result.candidate_count == 0
    assert result.reranker_model is None


@pytest.mark.asyncio
async def test_invalid_query_limits_and_provider_dimensions_fail_explicitly() -> None:
    """验证空白查询、越界 chunk_limit 与错长度查询向量都显式失败。

    前两者是调用方逻辑错误，静默返回空结果会让上层以为知识库没有命中；错长度向量则说明配置维度
    与真实模型不一致，此时拿着它去查库只会得到一批无意义的距离，显式失败远更安全。
    """

    repository = _StubRepository([], [])
    service = DocumentRetrievalService(repository, _StubEmbeddingProvider())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must not be blank"):
        await service.retrieve("   ")
    with pytest.raises(ValueError, match="chunk_limit must be between"):
        await service.retrieve("主键冲突", chunk_limit=0)

    mismatched = DocumentRetrievalService(
        repository,  # type: ignore[arg-type]
        _StubEmbeddingProvider(dimensions=8, emitted_dimensions=16),
    )
    with pytest.raises(ValueError, match="does not match provider dimensions"):
        await mismatched.retrieve("主键冲突")


def test_constructor_rejects_out_of_range_rerank_configuration() -> None:
    """验证非法重排倍数与融合权重在构造阶段失败，而不是留到首次检索。

    构造器不做 I/O，因此这类错误配置本可以在启动时就暴露；留到首次检索才失败意味着一次真实诊断
    请求被浪费，而且失败点距离配置来源已经很远。
    """

    repository = _StubRepository([], [])
    provider = _StubEmbeddingProvider()

    with pytest.raises(ValueError, match="rerank_candidate_multiplier"):
        DocumentRetrievalService(repository, provider, rerank_candidate_multiplier=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rerank_blend_weight"):
        DocumentRetrievalService(repository, provider, rerank_blend_weight=1.5)  # type: ignore[arg-type]
