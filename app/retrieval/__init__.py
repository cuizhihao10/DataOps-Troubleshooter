"""GraphRAG 与文档 RAG 两条知识通道的 embedding、双路种子、重排、混合评分与显式路径领域出口。

当前切片使用可替换 Provider 生成向量，由 pgvector 与全文查询独立召回，可选 cross-encoder 在
有界候选集上做二阶段精排，再保留分量完成去重评分、显式路径、预算化 Bundle 和消融模式。
文档通道与图通道并列导出：两者共享 Provider、重排器与评分诚实性不变量，但节点与切片是不同的
引用单元，因此模型和服务保持独立。统一出口不暴露 SQLAlchemy Record、HTTP 客户端或模型 SDK。
"""

from app.retrieval.budget import build_evidence_bundle
from app.retrieval.chunking import chunk_markdown_document
from app.retrieval.document_service import DocumentRetrievalService
from app.retrieval.documents import (
    DOCUMENT_RETRIEVAL_CONTRACT_ID,
    BundledDocumentChunk,
    DocumentChunk,
    DocumentLibrary,
    DocumentMetadata,
    DocumentRetrievalResult,
    DocumentScoringWeights,
    DocumentType,
    KnowledgeDocument,
    ScoredDocumentChunk,
    document_chunk_text,
    make_chunk_id,
)
from app.retrieval.embeddings import (
    BGE_M3_DIMENSIONS,
    BGE_M3_PROVIDER_ID,
    DETERMINISTIC_HASH_PROVIDER_ID,
    EmbeddingProvider,
    EmbeddingProviderError,
    create_embedding_provider,
    embed_document_library,
    embed_knowledge_bundle,
    knowledge_node_text,
)
from app.retrieval.models import (
    EvidenceBundleBudget,
    GraphEvidenceBundle,
    GraphPath,
    GraphRetrievalResult,
    HybridScoringWeights,
    HybridSeedMatch,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeType,
    KnowledgeRelationType,
    KnowledgeSeedBundle,
    LexicalSeedMatch,
    RetrievalChannel,
    RetrievalMode,
    ScoredGraphPath,
    VectorSeedMatch,
)
from app.retrieval.reranker import (
    BGE_RERANKER_V2_M3_PROVIDER_ID,
    DISABLED_RERANKER_PROVIDER_ID,
    RerankerError,
    RerankerProvider,
    create_reranker,
)
from app.retrieval.scoring import blend_scores, bounded_score

__all__ = [
    "BGE_M3_DIMENSIONS",
    "BGE_M3_PROVIDER_ID",
    "BGE_RERANKER_V2_M3_PROVIDER_ID",
    "DETERMINISTIC_HASH_PROVIDER_ID",
    "DISABLED_RERANKER_PROVIDER_ID",
    "DOCUMENT_RETRIEVAL_CONTRACT_ID",
    "BundledDocumentChunk",
    "DocumentChunk",
    "DocumentLibrary",
    "DocumentMetadata",
    "DocumentRetrievalResult",
    "DocumentRetrievalService",
    "DocumentScoringWeights",
    "DocumentType",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "EvidenceBundleBudget",
    "GraphEvidenceBundle",
    "GraphPath",
    "GraphRetrievalResult",
    "HybridScoringWeights",
    "HybridSeedMatch",
    "KnowledgeDocument",
    "KnowledgeEdge",
    "KnowledgeNode",
    "KnowledgeNodeType",
    "KnowledgeRelationType",
    "KnowledgeSeedBundle",
    "LexicalSeedMatch",
    "RerankerError",
    "RerankerProvider",
    "RetrievalChannel",
    "RetrievalMode",
    "ScoredDocumentChunk",
    "ScoredGraphPath",
    "VectorSeedMatch",
    "blend_scores",
    "bounded_score",
    "build_evidence_bundle",
    "chunk_markdown_document",
    "create_embedding_provider",
    "create_reranker",
    "document_chunk_text",
    "embed_document_library",
    "embed_knowledge_bundle",
    "knowledge_node_text",
    "make_chunk_id",
]
