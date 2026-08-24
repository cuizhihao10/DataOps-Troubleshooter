"""为文档 RAG 建立 documents 与 document_chunks 两张表。

知识图擅长回答"故障如何沿依赖传播"，但排障最终要给出可执行处置步骤，而这些步骤只存在于
Runbook/SOP/复盘文档里。本迁移因此新增独立的文档域：文档元数据与切片正文分表，切片沿用与
知识节点相同的 pgvector 列和全有或全无 embedding 约束，使两种检索可以共用同一个 Provider
契约与同一次向量空间过滤，而不必在一张表里混放两种语义单元。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20260716_0008"
down_revision = "20260716_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建文档与文档切片表，并补齐向量空间索引与全文检索表达式索引。

    切片表的外键指向文档主键并级联删除，保证重新导入文档时不会残留孤立向量；GIN 表达式索引用
    `op.execute` 创建，因为 Alembic 的 create_index 无法表达 to_tsvector 这类函数索引。
    """

    # 文档先于切片建立，因为切片表外键依赖 documents 主键；vector 扩展在 0001 已启用，
    # 这里不重复创建。
    op.create_table(
        "documents",
        sa.Column("doc_id", sa.String(length=100), primary_key=True),
        sa.Column("doc_type", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.String(length=50), nullable=False),
        sa.Column("reliability", sa.Float(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "doc_type IN ('runbook','sop','postmortem','faq')",
            name="ck_documents_type",
        ),
        sa.CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_documents_reliability",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(components) = 'array' AND jsonb_array_length(components) >= 1",
            name="ck_documents_components",
        ),
    )
    op.create_index("ix_documents_type", "documents", ["doc_type"])
    op.create_index("ix_documents_source", "documents", ["source_id"])

    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", sa.String(length=100), primary_key=True),
        sa.Column("doc_id", sa.String(length=100), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("embedding_provider", sa.String(length=100), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["doc_id"], ["documents.doc_id"], ondelete="CASCADE"),
        sa.CheckConstraint("ordinal >= 0", name="ck_document_chunks_ordinal"),
        sa.CheckConstraint("char_count >= 1", name="ck_document_chunks_char_count"),
        sa.CheckConstraint(
            "(embedding IS NULL AND embedding_provider IS NULL AND "
            "embedding_dimensions IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_provider IS NOT NULL AND "
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions)",
            name="ck_document_chunks_embedding_metadata",
        ),
        sa.UniqueConstraint("doc_id", "ordinal", name="uq_document_chunks_doc_ordinal"),
    )
    op.create_index("ix_document_chunks_doc", "document_chunks", ["doc_id"])
    op.create_index(
        "ix_document_chunks_embedding_space",
        "document_chunks",
        ["embedding_provider", "embedding_dimensions"],
    )
    # 标题路径与正文一起进入同一个 tsvector：SOP 的关键词常只出现在小节标题上，只索引正文会漏召回。
    op.execute(
        "CREATE INDEX ix_document_chunks_search ON document_chunks USING gin "
        "(to_tsvector('simple', coalesce(heading_path, '') || ' ' || coalesce(content, '')))"
    )


def downgrade() -> None:
    """按外键反序删除切片与文档表，并显式移除表达式索引。

    表达式索引不随 drop_index 的默认推断被识别，因此先显式删除再删表；切片表先于文档表删除，
    避免 PostgreSQL 因外键依赖拒绝 DDL。
    """

    op.drop_index("ix_document_chunks_search", table_name="document_chunks")
    op.drop_index("ix_document_chunks_embedding_space", table_name="document_chunks")
    op.drop_index("ix_document_chunks_doc", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_source", table_name="documents")
    op.drop_index("ix_documents_type", table_name="documents")
    op.drop_table("documents")
