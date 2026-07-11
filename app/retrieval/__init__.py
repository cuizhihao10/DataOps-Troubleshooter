"""GraphRAG embedding、双路种子、混合评分与显式路径领域出口。

当前切片使用可替换 Provider 生成向量，由 pgvector 与全文查询独立召回，再保留五项分量完成
去重评分、显式路径、预算化 Bundle 和消融模式。统一出口不暴露 SQLAlchemy Record 或模型 SDK。
"""

from app.retrieval.budget import build_evidence_bundle
from app.retrieval.embeddings import (
    EmbeddingProvider,
    create_embedding_provider,
    embed_knowledge_bundle,
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

__all__ = [
    "EmbeddingProvider",
    "EvidenceBundleBudget",
    "GraphEvidenceBundle",
    "GraphPath",
    "GraphRetrievalResult",
    "HybridScoringWeights",
    "HybridSeedMatch",
    "KnowledgeEdge",
    "KnowledgeNode",
    "KnowledgeNodeType",
    "KnowledgeRelationType",
    "KnowledgeSeedBundle",
    "LexicalSeedMatch",
    "RetrievalChannel",
    "RetrievalMode",
    "ScoredGraphPath",
    "VectorSeedMatch",
    "create_embedding_provider",
    "build_evidence_bundle",
    "embed_knowledge_bundle",
]
