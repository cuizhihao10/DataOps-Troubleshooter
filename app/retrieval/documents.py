"""文档 RAG 的领域契约：文档、确定性切片、混合评分与检索结果。

知识图回答"故障如何沿依赖传播"，但排障最后一步需要的是可执行处置步骤，而这些步骤只写在
Runbook/SOP/复盘里。本模块因此定义与 GraphRAG 平行的第二条知识通道：文档被确定性切分成
带标题路径的片段，片段是唯一的检索与引用单元，`dc_*` 引用可直接进入报告 evidence_refs。
切片沿用知识节点的全有或全无 embedding 约束，重排一致性不变量从 `app.retrieval.scoring`
共享而非复制，保证"最终分偏离一阶段分必须有二阶段分数解释"这条诚实性规则在两条通道里只有
一份实现。
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.retrieval.scoring import (
    RetrievalChannel,
    default_final_score,
    validate_rerank_consistency,
)

DOCUMENT_RETRIEVAL_CONTRACT_ID = "document-retrieval:v1"
MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 40
MAX_DOCUMENT_CHUNKS = 200


class DocumentType(StrEnum):
    """限定文档库允许的四类运维文档来源。

    Runbook 与 SOP 提供处置步骤，复盘提供已验证的因果链，FAQ 提供高频问答；四类都是人工资料，
    因此可靠性由人工声明而非模型推断。枚举值与数据库 CheckConstraint 一致，新增类型必须同步迁移。
    """

    RUNBOOK = "runbook"
    SOP = "sop"
    POSTMORTEM = "postmortem"
    FAQ = "faq"


class DocumentChunk(BaseModel):
    """表示一个带标题路径与可选向量的文档片段，是文档检索的最小可引用单元。

    `heading_path` 保留"文档标题 > 章节 > 小节"的层级，使报告引用能说明步骤出自哪一节，而不是
    只给出一段无上下文的正文；`ordinal` 保持文档内顺序，让相邻片段可以按需拼接回可读的处置流程。
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(pattern=r"^dc_[a-f0-9]{16}$")
    doc_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    ordinal: int = Field(ge=0)
    heading_path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)
    char_count: int = Field(ge=1, le=MAX_CHUNK_CHARS)
    embedding: list[float] | None = None
    embedding_provider: str | None = Field(default=None, min_length=1, max_length=100)
    embedding_dimensions: int | None = Field(default=None, ge=8, le=4096)

    @model_validator(mode="after")
    def validate_chunk_invariants(self) -> DocumentChunk:
        """保证 char_count 与正文长度一致，且向量三元组要么齐备要么全空。

        char_count 是数据库层的成本与预算依据，若它和正文脱节，上下文预算就会按错误长度裁剪证据；
        向量校验与知识节点完全一致，防止不同 Provider 空间或全零向量静默进入同一次 cosine 比较。
        """

        if self.char_count != len(self.content):
            raise ValueError("char_count must match the chunk content length")

        metadata_present = (
            self.embedding_provider is not None or self.embedding_dimensions is not None
        )
        if self.embedding is None:
            if metadata_present:
                raise ValueError("embedding metadata requires an embedding vector")
            return self

        if self.embedding_provider is None or self.embedding_dimensions is None:
            raise ValueError("embedding vector requires provider and dimensions metadata")
        if len(self.embedding) != self.embedding_dimensions:
            raise ValueError("embedding length must match embedding_dimensions")
        if not all(isfinite(value) for value in self.embedding):
            raise ValueError("embedding values must be finite")
        if not any(value != 0 for value in self.embedding):
            raise ValueError("embedding vector must not be all zeros")
        return self


class DocumentMetadata(BaseModel):
    """表示一份运维文档的类型、覆盖组件、来源与人工声明可靠性，不含正文。

    `reliability` 直接充当文档检索的 authority 因子：一份经过验证的复盘应当压过一条未评审的 FAQ，
    而这个判断只能来自人工声明而非模型推断。元数据单独成模型，使检索命中单个切片时无需回查同文档
    其它切片，也不必构造占位正文——检索结果里出现任何凭空拼装的片段都是不可接受的证据污染。
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    doc_type: DocumentType
    title: str = Field(min_length=1, max_length=300)
    components: list[str] = Field(min_length=1)
    source_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=50)
    reliability: float = Field(default=1, ge=0, le=1)


class KnowledgeDocument(DocumentMetadata):
    """在文档元数据之上附加完整切片序列，作为导入流程的原子单元。

    只有导入路径需要完整切片；检索路径一律使用父类元数据加单个命中切片。切片顺序与归属在此做
    模型级校验，避免坏数据要等到数据库外键或唯一约束才被发现，那时错误信息已无法指向具体文档。
    """

    chunks: list[DocumentChunk] = Field(min_length=1, max_length=MAX_DOCUMENT_CHUNKS)

    @model_validator(mode="after")
    def validate_chunk_sequence(self) -> KnowledgeDocument:
        """验证切片全部归属本文档、ID 唯一且序号从零开始连续递增。

        序号连续是"按 ordinal 拼回完整章节"这一读取方式成立的前提，同时也让数据库唯一约束成为
        真正的最后防线；ID 重复则会让 upsert 静默覆盖一段正文，而检索结果看不出任何异常。
        """

        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError(f"document {self.doc_id} contains duplicate chunk IDs")
        for expected, chunk in enumerate(self.chunks):
            if chunk.doc_id != self.doc_id:
                raise ValueError(f"chunk {chunk.chunk_id} does not belong to {self.doc_id}")
            if chunk.ordinal != expected:
                raise ValueError(f"document {self.doc_id} chunk ordinals must be contiguous")
        return self


class DocumentLibrary(BaseModel):
    """封装一个版本化文档种子集合，并在入库前拒绝重复文档 ID。

    与知识图种子对称：库级校验保证一次导入要么整体合法要么整体失败，不会出现"前半部分文档已写入、
    后半部分因主键冲突回滚"的中间态；`library_version` 让评测能标注结论依据的是哪一版文档语料。
    """

    model_config = ConfigDict(extra="forbid")

    library_version: str = Field(pattern=r"^document-seed:v[0-9]+$")
    documents: list[KnowledgeDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_documents(self) -> DocumentLibrary:
        """验证库内文档 ID 唯一，避免同一 doc_id 的两份正文互相覆盖。

        文档 upsert 按 doc_id 先删切片再插入，因此同一 ID 出现两次会让先写入的那份正文被静默丢弃，
        而检索结果只会少召回、不会报错——这类缺陷必须在加载阶段就暴露。
        """

        doc_ids = [document.doc_id for document in self.documents]
        if len(doc_ids) != len(set(doc_ids)):
            raise ValueError("document library contains duplicate document IDs")
        return self


class DocumentScoringWeights(BaseModel):
    """集中声明文档检索的三项可解释评分权重，并强制总和等于一。

    文档域刻意不复用 GraphRAG 的五因子：`path` 对没有关系边的片段没有意义，`freshness` 也无法从
    静态语料得到诚实取值，硬塞进去只会让权重看起来更复杂而不更准确。authority 取文档声明可靠性。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic: float = Field(default=0.60, ge=0, le=1)
    lexical: float = Field(default=0.25, ge=0, le=1)
    authority: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def validate_total_weight(self) -> DocumentScoringWeights:
        """验证三项权重之和在浮点容差内等于一，并返回不可变配置对象。

        与 GraphRAG 权重同样拒绝自动归一化：一个错配的权重被静默修正后，评测报告里的分数区间仍
        看起来正常，却已经无法与文档基线比较，因此必须在启动阶段显式失败。
        """

        total = self.semantic + self.lexical + self.authority
        if abs(total - 1.0) > 1e-9:
            raise ValueError("document scoring weights must sum to 1.0")
        return self


class LexicalChunkMatch(BaseModel):
    """把全文召回的文档片段与非负 lexical score 绑定为候选。

    分数来自 PostgreSQL ts_rank 与标识符 LIKE bonus，只用于当前候选排序，不冒充语义相似度；
    随片段一起携带所属文档，使 authority 因子和最终引用脚注都无需二次查询即可组装。
    """

    model_config = ConfigDict(extra="forbid")

    document: DocumentMetadata
    chunk: DocumentChunk
    lexical_score: float = Field(ge=0)


class VectorChunkMatch(BaseModel):
    """把 pgvector cosine 相似度与对应文档片段绑定为语义候选。

    `semantic_score` 已从 cosine distance 转换并裁剪到零到一，原始向量不进入检索结果，避免上下文
    重复携带大数组并泄漏派生模型特征；Provider ID 与维度随匹配返回，供 trace 说明比较发生在哪个
    向量空间。
    """

    model_config = ConfigDict(extra="forbid")

    document: DocumentMetadata
    chunk: DocumentChunk
    embedding_provider: str = Field(min_length=1, max_length=100)
    embedding_dimensions: int = Field(ge=8, le=4096)
    semantic_score: float = Field(ge=0, le=1)


class ScoredDocumentChunk(BaseModel):
    """表示两通道合并、并可选经 cross-encoder 重排后的可解释文档片段评分。

    三层分数分开保存的理由与 GraphRAG 一致：`hybrid_score` 是三因子加权的一阶段分，`rerank_score`
    是二阶段 cross-encoder 分，`final_score` 是显式融合结果，只有三者都在才能判断名次变化的来源。
    """

    model_config = ConfigDict(extra="forbid")

    document: DocumentMetadata
    chunk: DocumentChunk
    channels: list[RetrievalChannel] = Field(min_length=1)
    semantic_score: float = Field(default=0, ge=0, le=1)
    lexical_score: float = Field(default=0, ge=0, le=1)
    authority_score: float = Field(ge=0, le=1)
    hybrid_score: float = Field(ge=0, le=1)
    rerank_score: float | None = Field(default=None, ge=0, le=1)
    final_score: float = Field(ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def default_final_score(cls, data: object) -> object:
        """未显式给出 `final_score` 时让它等于 `hybrid_score`，表示本次没有跑第二阶段。

        复用 GraphRAG 的同一个补齐函数而不是重写一遍，避免两条检索通道对"未重排"给出不同表示，
        否则同一批断言在文档侧和图侧会得到不一致的默认排序值。
        """

        return default_final_score(data)

    @model_validator(mode="after")
    def validate_rerank_consistency(self) -> ScoredDocumentChunk:
        """禁止在没有 `rerank_score` 的情况下让 `final_score` 偏离 `hybrid_score`。

        文档片段的最终排序直接决定哪几条处置步骤进入报告，因此这条不变量必须与图侧共用一份实现：
        任何排序改写都要留下可核对的二阶段分数，而不能由某个中间步骤悄悄调权后无从追溯。
        """

        validate_rerank_consistency(self.hybrid_score, self.rerank_score, self.final_score)
        return self


class DocumentRetrievalResult(BaseModel):
    """表示一次文档检索的查询、命中片段与二阶段重排溯源信息。

    空 chunks 是合法的"未召回"结果，调用方应据此声明不确定性而不是编造处置步骤；`reranker_model`
    为空即表示只跑了一阶段，`candidate_count` 记录重排前的候选规模，使重排增益在评测里有分母。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: Literal["document-retrieval:v1"] = DOCUMENT_RETRIEVAL_CONTRACT_ID
    query: str = Field(min_length=1, max_length=2000)
    chunk_limit: int = Field(default=4, ge=1, le=20)
    embedding_provider: str = Field(min_length=1, max_length=100)
    reranker_model: str | None = Field(default=None, min_length=1, max_length=200)
    candidate_count: int = Field(default=0, ge=0)
    score_weights: DocumentScoringWeights
    rerank_blend_weight: float = Field(default=0, ge=0, le=1)
    chunks: list[ScoredDocumentChunk] = Field(default_factory=list)


class BundledDocumentChunk(BaseModel):
    """表示进入 Planner 上下文的紧凑文档片段证据及其稳定引用。

    只保留标题路径、正文、来源与最终检索分：文档 RAG 的价值是"可执行步骤 + 出处"，把 embedding、
    切片统计或数据库时间戳一起注入只会挤占预算。`dc_<hash>` 可直接作为报告 evidence_refs 使用。
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^dc_[a-f0-9]{16}$")
    chunk_id: str = Field(pattern=r"^dc_[a-f0-9]{16}$")
    doc_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    doc_type: DocumentType
    title: str = Field(min_length=1, max_length=300)
    heading_path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)
    source_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=50)
    reliability: float = Field(ge=0, le=1)
    retrieval_score: float = Field(ge=0, le=1)


def document_chunk_text(document: DocumentMetadata, chunk: DocumentChunk) -> str:
    """把文档标题、标题路径与片段正文拼成用于 embedding 与重排的检索文本。

    标题与章节路径必须参与编码：SOP 的关键词经常只出现在小节标题上（"限流阈值调整"），只编码正文
    会让这类片段在语义通道彻底消失。拼接顺序固定，因此同一片段在入库与查询时得到完全相同的文本。
    """

    return "\n".join((document.title, chunk.heading_path, chunk.content))


def make_chunk_id(doc_id: str, ordinal: int) -> str:
    """按文档 ID 与序号生成稳定的 `dc_*` 片段引用 ID。

    使用摘要而不是 `doc_id:ordinal` 拼接，是为了让引用 ID 长度有界且字符集固定（正则可校验），
    同时保持确定性：同一文档重新导入后引用不变，历史报告里的 `dc_*` 脚注仍然指向同一段正文。
    """

    digest = sha256(f"{doc_id}|{ordinal}".encode()).hexdigest()
    return f"dc_{digest[:16]}"
