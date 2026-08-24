"""验证文档 RAG 领域模型的长度、向量、序号与权重不变量在构造阶段就拒绝坏数据。

文档通道的所有坏数据都有一个共同特征：不会报错，只会让检索少召回或让引用指向别的正文。切片
`char_count` 与正文脱节会让上下文预算按错误长度裁剪；向量三元组不齐会让不同 Provider 空间进入
同一次 cosine 比较；序号断裂会让"按 ordinal 拼回章节"这一读法失效；文档 ID 重复会让 upsert 静默
覆盖一份正文。这些测试因此逐条锁定 Pydantic 校验，并锁定"final_score 偏离 hybrid_score 必须有
rerank_score 解释"这条与图侧共享的诚实性不变量。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.retrieval.documents import (
    DocumentChunk,
    DocumentLibrary,
    DocumentMetadata,
    DocumentScoringWeights,
    DocumentType,
    KnowledgeDocument,
    ScoredDocumentChunk,
    document_chunk_text,
    make_chunk_id,
)
from app.retrieval.scoring import RetrievalChannel

_DOC_ID = "runbook_flashsync_primary_key_conflict"


def _chunk(ordinal: int = 0, **overrides: object) -> DocumentChunk:
    """构造一个合法切片并允许逐字段覆盖，供各条不变量用例复用。

    默认取值刻意全部合法，因此任何用例失败都能确定原因来自它覆盖的那一个字段；`chunk_id` 由
    `make_chunk_id` 生成而不是写死，保证测试与生产使用同一套引用规则。
    """

    payload: dict[str, object] = {
        "chunk_id": make_chunk_id(_DOC_ID, ordinal),
        "doc_id": _DOC_ID,
        "ordinal": ordinal,
        "heading_path": "FlashSync 主键冲突处置手册 > 处置步骤",
        "content": "暂停同步任务并记录 offset。",
        "char_count": len("暂停同步任务并记录 offset。"),
    }
    payload.update(overrides)
    return DocumentChunk.model_validate(payload)


def _metadata(**overrides: object) -> DocumentMetadata:
    """构造一份合法文档元数据，默认可靠性小于一以便区分 authority 因子的来源。

    可靠性取 0.9 而不是默认 1，使断言能够证明 authority 分数真的来自文档声明值，而不是恰好等于
    某个上限常量；其余字段与切片测试保持一致，减少跨用例的心智负担。
    """

    payload: dict[str, object] = {
        "doc_id": _DOC_ID,
        "doc_type": DocumentType.RUNBOOK,
        "title": "FlashSync 主键冲突处置手册",
        "components": ["FlashSync"],
        "source_id": "synthetic-runbook-001",
        "revision": "v1.3",
        "reliability": 0.9,
    }
    payload.update(overrides)
    return DocumentMetadata.model_validate(payload)


def test_char_count_must_match_the_content_length() -> None:
    """验证 `char_count` 与正文长度不一致时切片构造失败。

    该字段是数据库成本统计与上下文预算的依据；一旦它和正文脱节，预算会按错误长度裁剪证据，
    表现是"明明没超预算却少了一条步骤"，从外部几乎无法诊断，因此必须在模型层直接拒绝。
    """

    with pytest.raises(ValidationError, match="char_count must match"):
        _chunk(char_count=3)


@pytest.mark.parametrize(
    "overrides",
    [
        {"embedding_provider": "deterministic-hash:v1"},
        {"embedding": [0.1, 0.2], "embedding_dimensions": 2},
        {
            "embedding": [0.1, 0.2],
            "embedding_provider": "deterministic-hash:v1",
            "embedding_dimensions": 3,
        },
        {
            "embedding": [0.0, 0.0],
            "embedding_provider": "deterministic-hash:v1",
            "embedding_dimensions": 2,
        },
        {
            "embedding": [float("inf"), 0.2],
            "embedding_provider": "deterministic-hash:v1",
            "embedding_dimensions": 2,
        },
    ],
    ids=[
        "metadata-without-vector",
        "vector-without-provider",
        "dimension-mismatch",
        "all-zero-vector",
        "non-finite-value",
    ],
)
def test_embedding_triple_is_all_or_nothing_and_numerically_valid(
    overrides: dict[str, object],
) -> None:
    """验证向量、Provider 与维度必须齐备，且向量不得全零或含非有限值。

    半个向量空间是文档检索最危险的状态：pgvector 会照常返回排序结果，只是把两个数学空间的距离
    当成语义相似度。全零向量的 cosine 无定义、非有限值会污染整批排序，两者同样只能在写入前拒绝。
    """

    with pytest.raises(ValidationError):
        _chunk(**overrides)


def test_document_rejects_foreign_duplicate_and_non_contiguous_chunks() -> None:
    """验证切片归属错误、ID 重复与序号断裂都在文档层失败。

    这三种坏数据在数据库层只会触发外键或唯一约束，那时错误信息已无法指向具体文档；更糟的是
    ID 重复会让 upsert 静默覆盖一段正文，检索结果只是少一条步骤而不报任何错。
    """

    metadata = {
        "doc_id": _DOC_ID,
        "doc_type": DocumentType.RUNBOOK,
        "title": "FlashSync 主键冲突处置手册",
        "components": ["FlashSync"],
        "source_id": "synthetic-runbook-001",
        "revision": "v1.3",
    }

    with pytest.raises(ValidationError, match="does not belong to"):
        KnowledgeDocument(**metadata, chunks=[_chunk(0, doc_id="sop_other_document")])
    with pytest.raises(ValidationError, match="duplicate chunk IDs"):
        KnowledgeDocument(**metadata, chunks=[_chunk(0), _chunk(0)])
    with pytest.raises(ValidationError, match="ordinals must be contiguous"):
        KnowledgeDocument(**metadata, chunks=[_chunk(0), _chunk(2)])

    document = KnowledgeDocument(**metadata, chunks=[_chunk(0), _chunk(1)])
    assert [chunk.ordinal for chunk in document.chunks] == [0, 1]


def test_library_rejects_duplicate_document_ids_and_bad_version() -> None:
    """验证库内重复 doc_id 与不合规 `library_version` 都在加载阶段失败。

    文档 upsert 按 doc_id 先删切片再整批插入，因此同一 ID 出现两次会让先写入的那份正文被静默
    丢弃；版本号格式受约束，是为了让评测报告能准确标注结论依据的是哪一版语料。
    """

    document = KnowledgeDocument(
        doc_id=_DOC_ID,
        doc_type=DocumentType.RUNBOOK,
        title="FlashSync 主键冲突处置手册",
        components=["FlashSync"],
        source_id="synthetic-runbook-001",
        revision="v1.3",
        chunks=[_chunk(0)],
    )

    with pytest.raises(ValidationError, match="duplicate document IDs"):
        DocumentLibrary(library_version="document-seed:v1", documents=[document, document])
    with pytest.raises(ValidationError):
        DocumentLibrary(library_version="v1", documents=[document])

    library = DocumentLibrary(library_version="document-seed:v1", documents=[document])
    assert library.documents[0].doc_id == _DOC_ID


def test_scoring_weights_must_sum_to_one_and_stay_immutable() -> None:
    """验证三项文档权重之和必须等于一，且配置对象构造后不可修改。

    拒绝自动归一化的理由与图侧一致：错配权重被静默修正后，分数区间看起来仍然正常，却已经无法
    与文档基线比较。冻结模型则防止某个节点在运行中临时调权，让同一次运行出现两套评分口径。
    """

    with pytest.raises(ValidationError, match="must sum to 1.0"):
        DocumentScoringWeights(semantic=0.6, lexical=0.25, authority=0.2)

    weights = DocumentScoringWeights()
    assert (weights.semantic, weights.lexical, weights.authority) == (0.60, 0.25, 0.15)
    with pytest.raises(ValidationError):
        weights.semantic = 0.5  # type: ignore[misc]


def test_final_score_may_only_deviate_from_hybrid_with_a_rerank_score() -> None:
    """验证缺少 `rerank_score` 时 `final_score` 不得偏离 `hybrid_score`，缺省时自动补齐。

    文档片段的最终排序决定哪几条处置步骤进入报告，任何排序改写都必须留下可核对的二阶段分数，
    否则中间步骤悄悄调权后无从追溯。该不变量与 GraphRAG 共用 `app.retrieval.scoring` 的实现。
    """

    payload: dict[str, object] = {
        "document": _metadata(),
        "chunk": _chunk(),
        "channels": [RetrievalChannel.VECTOR],
        "semantic_score": 0.8,
        "authority_score": 0.9,
        "hybrid_score": 0.62,
    }

    unranked = ScoredDocumentChunk.model_validate(payload)
    assert unranked.rerank_score is None
    assert unranked.final_score == 0.62

    with pytest.raises(ValidationError):
        ScoredDocumentChunk.model_validate({**payload, "final_score": 0.9})

    reranked = ScoredDocumentChunk.model_validate(
        {**payload, "rerank_score": 0.95, "final_score": 0.752}
    )
    assert reranked.final_score == 0.752


def test_embedding_text_includes_title_and_heading_path() -> None:
    """验证用于 embedding 与重排的检索文本包含文档标题与标题路径，且拼接顺序固定。

    SOP 的关键词经常只出现在小节标题上（例如"限流阈值调整"），只编码正文会让这类片段在语义
    通道彻底消失。顺序固定则保证同一片段在入库与查询时得到逐字节相同的文本，否则两侧向量不可比。
    """

    document = _metadata()
    chunk = _chunk()

    text = document_chunk_text(document, chunk)

    assert text == f"{document.title}\n{chunk.heading_path}\n{chunk.content}"
