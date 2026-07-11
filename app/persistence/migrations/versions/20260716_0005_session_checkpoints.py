"""创建版本化短期会话 checkpoint 表。

Revision ID: 20260716_0005
Revises: 20260715_0004
Create Date: 2026-07-16

表只保留每个 session 最新的公开状态快照；成功 run 与快照由应用在同一事务提交，失败 run 不会
覆盖已可用上下文。JSONB 的详细结构由 ``session-checkpoint:v1`` Pydantic 与 Schema 测试约束。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260716_0005"
down_revision = "20260715_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建一会话一快照表，并绑定来源 completed run 的可追溯外键。

    session_id 主键实现原子覆盖最新版本；source_run 唯一约束防止跨会话复用同一结果。版本正数
    约束和 JSONB 非空约束作为绕过 ORM 直接写入时的数据库防线。
    """

    # agent_runs 已由上一迁移创建，因此可在同一张新表上同时建立 session 与来源 run 外键。
    op.create_table(
        "session_checkpoints",
        sa.Column("session_id", sa.String(length=100), primary_key=True),
        sa.Column("source_run_id", sa.String(length=100), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
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
            "checkpoint_version >= 1",
            name="ck_session_checkpoints_version",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["diagnosis_sessions.session_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["agent_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_run_id",
            name="uq_session_checkpoints_source_run",
        ),
    )


def downgrade() -> None:
    """删除短期 checkpoint，不修改 session、run、事件或长期案例记忆。

    回退会失去同 session 追问恢复能力，但上一迁移的独立 run 仍可读取；单表无下游外键，因此
    直接删除即可满足 DDL 依赖顺序。
    """

    op.drop_table("session_checkpoints")
