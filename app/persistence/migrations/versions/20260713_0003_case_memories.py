"""创建经审计案例记忆、证据关联和 pgvector 去重存储。

Revision ID: 20260713_0003
Revises: 20260712_0002
Create Date: 2026-07-13

案例向量沿用可配置维度方案，并用 Provider/维度元数据隔离数学空间；JSONB 列保存领域列表，
memory_evidence 记录每个 run 的来源引用以支持幂等 occurrence_count。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20260713_0003"
down_revision = "20260712_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 case_memories、memory_evidence 及状态/向量空间索引和约束。

    先建主案例表，再建带级联外键的证据关联；签名唯一约束承担精确去重并发防线，向量维度约束
    阻止 Provider 元数据与实际 pgvector 长度漂移。Alembic 事务保证失败时不留下半张表。
    """

    # 主表必须先于关联表建立；vector 扩展已由首个迁移创建，此处只声明新列类型。
    op.create_table(
        "case_memories",
        sa.Column("memory_id", sa.String(length=100), primary_key=True),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("symptoms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("fault_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("solution_steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("embedding_provider", sa.String(length=100), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
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
            "status IN ('pending','confirmed','rejected')",
            name="ck_case_memories_status",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 1",
            name="ck_case_memories_occurrence_count",
        ),
        sa.CheckConstraint(
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions",
            name="ck_case_memories_embedding_dimensions",
        ),
        sa.UniqueConstraint("signature", name="uq_case_memories_signature"),
    )
    op.create_index("ix_case_memories_status", "case_memories", ["status"])
    op.create_index(
        "ix_case_memories_embedding_space",
        "case_memories",
        ["embedding_provider", "embedding_dimensions"],
    )

    # 复合主键使同一 run/evidence 重放幂等，同时保留不同 run 对同一记忆的独立审计关联。
    op.create_table(
        "memory_evidence",
        sa.Column("memory_id", sa.String(length=100), nullable=False),
        sa.Column("evidence_ref", sa.String(length=100), nullable=False),
        sa.Column("source_run_id", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["case_memories.memory_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "memory_id",
            "evidence_ref",
            "source_run_id",
            name="pk_memory_evidence",
        ),
    )
    op.create_index(
        "ix_memory_evidence_source_run",
        "memory_evidence",
        ["source_run_id"],
    )


def downgrade() -> None:
    """按外键依赖反序删除证据关联和案例记忆表。

    先删 memory_evidence，再删 case_memories；vector 扩展和知识图表仍由更早迁移拥有，不在此移除。
    回退会丢失长期记忆，只适用于开发或测试环境。
    """

    # 关联表依赖案例主键，必须先删除以满足 PostgreSQL 外键顺序。
    op.drop_table("memory_evidence")
    op.drop_table("case_memories")
