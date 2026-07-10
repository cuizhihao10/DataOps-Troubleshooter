"""Create pgvector knowledge graph tables.

Revision ID: 20260710_0001
Revises:
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20260710_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 pgvector 扩展、知识节点/边表及完整性和检索索引。

    迁移先确保 vector 类型可用，再建节点表与全文 GIN 索引，最后建依赖节点外键的边表。数据库
    约束重复领域 Schema 的核心不变量，以保护脚本或未来服务直接写库的路径；任一步失败由 Alembic
    事务回滚，不会留下可被 API 当作完整图使用的半成品结构。
    """

    # 扩展必须先创建，否则后续 embedding Vector 列在解析 DDL 时就会失败。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 节点先于边建立，因为边表的两个外键都依赖 knowledge_nodes 主键。
    op.create_table(
        "knowledge_nodes",
        sa.Column("node_id", sa.String(length=100), primary_key=True),
        sa.Column("node_type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("source_span", sa.Text(), nullable=False),
        sa.Column("reliability", sa.Float(), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "node_type IN "
            "('component','task','dataset','symptom','root_cause','solution','case','sop')",
            name="ck_knowledge_nodes_type",
        ),
        sa.CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_knowledge_nodes_reliability",
        ),
    )
    # 普通索引服务类型/来源过滤，表达式 GIN 索引服务当前 lexical seed 召回。
    op.create_index("ix_knowledge_nodes_type", "knowledge_nodes", ["node_type"])
    op.create_index("ix_knowledge_nodes_source", "knowledge_nodes", ["source_id"])
    op.execute(
        "CREATE INDEX ix_knowledge_nodes_search ON knowledge_nodes USING gin "
        "(to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(content, '') || "
        "' ' || coalesce(aliases::text, '')))"
    )

    # 边表显式保存有向关系、来源和权重，使 GraphRAG 能返回可引用路径而非文本暗示。
    op.create_table(
        "knowledge_edges",
        sa.Column("edge_id", sa.String(length=100), primary_key=True),
        sa.Column("from_node_id", sa.String(length=100), nullable=False),
        sa.Column("to_node_id", sa.String(length=100), nullable=False),
        sa.Column("relation_type", sa.String(length=30), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("source_span", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "relation_type IN "
            "('RUNS_ON','DEPENDS_ON','PRODUCES','CONSUMES','MANIFESTS_AS',"
            "'CAUSED_BY','RESOLVED_BY','SIMILAR_TO')",
            name="ck_knowledge_edges_relation_type",
        ),
        sa.CheckConstraint(
            "weight > 0 AND weight <= 1",
            name="ck_knowledge_edges_weight",
        ),
        sa.CheckConstraint(
            "from_node_id <> to_node_id",
            name="ck_knowledge_edges_no_self_loop",
        ),
        sa.ForeignKeyConstraint(
            ["from_node_id"],
            ["knowledge_nodes.node_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_node_id"],
            ["knowledge_nodes.node_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "from_node_id",
            "to_node_id",
            "relation_type",
            "source_id",
            name="uq_knowledge_edges_source_relation",
        ),
    )
    op.create_index("ix_knowledge_edges_from", "knowledge_edges", ["from_node_id"])
    op.create_index("ix_knowledge_edges_to", "knowledge_edges", ["to_node_id"])
    op.create_index(
        "ix_knowledge_edges_relation",
        "knowledge_edges",
        ["relation_type"],
    )


def downgrade() -> None:
    """按外键依赖反序删除图表和全文索引，回退首个知识图迁移。

    先删边表避免外键引用阻止节点删除，再显式删除节点表达式索引和节点表。vector 扩展可能被同库
    其他对象共享，因此不在 downgrade 中删除；回退会丢失知识图数据，只适用于开发/测试环境。
    """

    # 删除顺序与 upgrade 相反：先移除依赖节点的边，再移除节点索引和节点本身。
    op.drop_table("knowledge_edges")
    op.drop_index("ix_knowledge_nodes_search", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
