"""知识节点与关系边的 SQLAlchemy 表映射。

表级约束重复验证领域枚举、权重范围和禁止自环，形成数据库最后一道防线。节点包含可空
pgvector 字段，为下一切片的语义召回预留存储，但当前不会把空向量宣称为已完成检索。
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class KnowledgeNodeRecord(Base):
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
        Index("ix_knowledge_nodes_type", "node_type"),
        Index("ix_knowledge_nodes_source", "source_id"),
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
