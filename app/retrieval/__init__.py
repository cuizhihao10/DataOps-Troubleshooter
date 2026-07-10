"""GraphRAG embedding、双路种子、混合评分与显式路径领域出口。

当前切片使用可替换 Provider 生成向量，由 pgvector 与全文查询独立召回，再保留五项分量完成
去重评分并扩展显式路径。统一出口仅暴露稳定领域契约，不暴露 SQLAlchemy Record 或模型 SDK。
"""

from app.retrieval.embeddings import (
    EmbeddingProvider,
    create_embedding_provider,
    embed_knowledge_bundle,
)
from app.retrieval.models import (
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
    ScoredGraphPath,
    VectorSeedMatch,
)

__all__ = [
    "EmbeddingProvider",
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
    "ScoredGraphPath",
    "VectorSeedMatch",
    "create_embedding_provider",
    "embed_knowledge_bundle",
]
