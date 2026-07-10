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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
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
    op.create_index("ix_knowledge_nodes_type", "knowledge_nodes", ["node_type"])
    op.create_index("ix_knowledge_nodes_source", "knowledge_nodes", ["source_id"])
    op.execute(
        "CREATE INDEX ix_knowledge_nodes_search ON knowledge_nodes USING gin "
        "(to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(content, '') || "
        "' ' || coalesce(aliases::text, '')))"
    )

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
    op.drop_table("knowledge_edges")
    op.drop_index("ix_knowledge_nodes_search", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
