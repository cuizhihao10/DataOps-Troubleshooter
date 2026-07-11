"""知识图与受控长期案例记忆的 SQLAlchemy 表映射。

表级约束重复验证领域枚举、向量空间、状态和计数，形成数据库最后一道防线。知识图与案例记忆
共享 PostgreSQL/pgvector，但保持独立表和仓储职责。
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """为本项目 ORM 映射提供统一 SQLAlchemy metadata 注册中心。

    Alembic 从 `Base.metadata` 发现表结构，运行时仓储则使用具体 Record；基类不承载领域行为，
    从而保持 Pydantic 领域模型与数据库持久化模型分离。
    """

    pass


class KnowledgeNodeRecord(Base):
    """把知识图节点映射到 PostgreSQL，并在数据库层重复关键完整性约束。

    JSONB 保存别名，GIN 表达式索引由迁移创建，Vector 可空字段为后续语义召回预留。类型与可靠性
    CheckConstraint 防止绕过 Pydantic 的其他写入者污染表；Record 只负责持久化，不生成证据结论。
    """

    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        CheckConstraint(
            "node_type IN "
            "('component','task','dataset','symptom','root_cause','solution','case','sop')",
            name="ck_knowledge_nodes_type",
        ),
        CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_knowledge_nodes_reliability",
        ),
        CheckConstraint(
            "(embedding IS NULL AND embedding_provider IS NULL AND "
            "embedding_dimensions IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_provider IS NOT NULL AND "
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions)",
            name="ck_knowledge_nodes_embedding_metadata",
        ),
        Index("ix_knowledge_nodes_type", "node_type"),
        Index("ix_knowledge_nodes_source", "source_id"),
        Index(
            "ix_knowledge_nodes_embedding_space",
            "embedding_provider",
            "embedding_dimensions",
        ),
    )

    node_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_span: Mapped[str] = mapped_column(Text, nullable=False)
    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KnowledgeEdgeRecord(Base):
    """把有向知识关系映射到 PostgreSQL，并禁止非法类型、权重、自环和重复来源边。

    两端外键使用级联删除保证删节点不留悬空边；唯一约束允许不同来源描述同一关系，同时阻止同一
    来源重复写入。边权用于路径评分而非概率证明，最终结论仍需来源与实时 Observation 支撑。
    """

    __tablename__ = "knowledge_edges"
    __table_args__ = (
        CheckConstraint(
            "relation_type IN "
            "('RUNS_ON','DEPENDS_ON','PRODUCES','CONSUMES','MANIFESTS_AS',"
            "'CAUSED_BY','RESOLVED_BY','SIMILAR_TO')",
            name="ck_knowledge_edges_relation_type",
        ),
        CheckConstraint(
            "weight > 0 AND weight <= 1",
            name="ck_knowledge_edges_weight",
        ),
        CheckConstraint(
            "from_node_id <> to_node_id",
            name="ck_knowledge_edges_no_self_loop",
        ),
        UniqueConstraint(
            "from_node_id",
            "to_node_id",
            "relation_type",
            "source_id",
            name="uq_knowledge_edges_source_relation",
        ),
        Index("ix_knowledge_edges_from", "from_node_id"),
        Index("ix_knowledge_edges_to", "to_node_id"),
        Index("ix_knowledge_edges_relation", "relation_type"),
    )

    edge_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    from_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    to_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_span: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaseMemoryRecord(Base):
    """把结构化案例、确认状态、去重签名和 embedding 映射到 PostgreSQL。

    JSONB 保存有序列表字段，Vector 保存不进入领域/API 的检索向量；签名唯一约束提供精确去重，
    状态/计数/向量 CheckConstraint 防止绕过 Service 的直接写入污染默认 confirmed 召回。
    """

    __tablename__ = "case_memories"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','confirmed','rejected')",
            name="ck_case_memories_status",
        ),
        CheckConstraint(
            "occurrence_count >= 1",
            name="ck_case_memories_occurrence_count",
        ),
        CheckConstraint(
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions",
            name="ck_case_memories_embedding_dimensions",
        ),
        UniqueConstraint("signature", name="uq_case_memories_signature"),
        Index("ix_case_memories_status", "status"),
        Index("ix_case_memories_embedding_space", "embedding_provider", "embedding_dimensions"),
    )

    memory_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    symptoms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    fault_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    solution_steps: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    components: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MemoryEvidenceRecord(Base):
    """保存案例与每次来源 run 的 Evidence 引用关联，支持幂等 occurrence 统计。

    三列复合主键允许不同运行引用同名证据，同时阻止同一运行重复插入同一引用；source_run_id
    让 Service 判断重放是否已经计数。删除案例时级联清理关联，不遗留不可追溯证据。
    """

    __tablename__ = "memory_evidence"
    __table_args__ = (Index("ix_memory_evidence_source_run", "source_run_id"),)

    memory_id: Mapped[str] = mapped_column(
        ForeignKey("case_memories.memory_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_ref: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
