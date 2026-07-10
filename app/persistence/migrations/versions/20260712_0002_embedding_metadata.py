"""为知识节点向量增加 Provider 溯源、维度约束和兼容空间索引。

Revision ID: 20260712_0002
Revises: 20260710_0001
Create Date: 2026-07-12

pgvector 可以在无固定列维度时保存多种长度，但 cosine 查询不能安全混合不同模型空间。本迁移
不锁死单一维度，而是逐行记录 Provider 与维度，并用约束保证向量和元数据同步存在。
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0002"
down_revision = "20260710_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增 embedding Provider/维度字段、完整性约束和过滤索引。

    两列先以可空形式加入，兼容首个迁移中尚未嵌入的历史节点；CheckConstraint 随后要求向量、
    Provider 和维度三者同时存在或同时为空，并用 `vector_dims` 验证真实长度。复合索引支持查询
    先筛选兼容向量空间再计算 cosine distance，避免模型切换后跨空间比较。
    """

    # 可空列允许旧的 lexical-only 数据完成迁移，后续 seed 命令会在单事务中回填全部向量。
    op.add_column(
        "knowledge_nodes",
        sa.Column("embedding_provider", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "knowledge_nodes",
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
    )

    # 数据库约束是绕过 Pydantic 直接写库时的最后防线，尤其防止只更新向量未更新 Provider。
    op.create_check_constraint(
        "ck_knowledge_nodes_embedding_metadata",
        "knowledge_nodes",
        "(embedding IS NULL AND embedding_provider IS NULL AND "
        "embedding_dimensions IS NULL) OR "
        "(embedding IS NOT NULL AND embedding_provider IS NOT NULL AND "
        "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions)",
    )
    op.create_index(
        "ix_knowledge_nodes_embedding_space",
        "knowledge_nodes",
        ["embedding_provider", "embedding_dimensions"],
    )


def downgrade() -> None:
    """按依赖反序删除向量空间索引、完整性约束和两列溯源元数据。

    回退不会删除原有 embedding 列，但会失去判断向量由哪个 Provider 和维度生成的能力，因此只应
    用于开发测试。先删索引和约束再删列，避免 PostgreSQL 因依赖对象阻止 DDL。
    """

    # 索引与约束引用新列，必须在列之前移除；原始向量数据仍保留在 0001 的 embedding 列中。
    op.drop_index("ix_knowledge_nodes_embedding_space", table_name="knowledge_nodes")
    op.drop_constraint(
        "ck_knowledge_nodes_embedding_metadata",
        "knowledge_nodes",
        type_="check",
    )
    op.drop_column("knowledge_nodes", "embedding_dimensions")
    op.drop_column("knowledge_nodes", "embedding_provider")
