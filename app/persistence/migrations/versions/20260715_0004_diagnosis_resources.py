"""创建资源化诊断 session、agent run 和公开事件持久化表。

Revision ID: 20260715_0004
Revises: 20260713_0003
Create Date: 2026-07-15

首版同步执行 workflow，但仍把输入、终态和事件保存为可轮询资源；状态约束防止部分结果被误报为
completed。表中不保存模型原始思维链、供应商响应体或凭据。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260715_0004"
down_revision = "20260713_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """按会话 → run → event 外键顺序创建三张资源表和完整性约束。

    run 终态 CheckConstraint 将 status 与 result/error/completed_at 原子绑定；Alembic 事务保证
    任一表或索引失败时整体回滚，不留下 API 可见但无法关联的半套资源。
    """

    # 会话是 run 的所有者，先建立主表才能让后续级联外键在同一迁移事务中生效。
    op.create_table(
        "diagnosis_sessions",
        sa.Column("session_id", sa.String(length=100), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("last_user_query_summary", sa.String(length=500), nullable=True),
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
    )
    op.create_index(
        "ix_diagnosis_sessions_updated_at",
        "diagnosis_sessions",
        ["updated_at"],
    )

    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(length=100), primary_key=True),
        sa.Column("session_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("user_query", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=50), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("history_trigger", sa.String(length=30), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','failed')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "history_trigger IN "
            "('not_requested','user_requested','planner_validation','reusable_signature')",
            name="ck_agent_runs_history_trigger",
        ),
        sa.CheckConstraint(
            "intent IN ('single_component_diagnosis','cross_component_diagnosis')",
            name="ck_agent_runs_intent",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(components) = 'array' AND jsonb_array_length(components) >= 1",
            name="ck_agent_runs_components",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND result IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND result IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'failed' AND result IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_agent_runs_terminal_payload",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["diagnosis_sessions.session_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_agent_runs_session_created",
        "agent_runs",
        ["session_id", "created_at"],
    )
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])

    # 事件最后创建并依赖 run；唯一 run/sequence 是轮询时间线无重复、无歧义的数据库防线。
    op.create_table(
        "run_events",
        sa.Column("event_id", sa.String(length=100), primary_key=True),
        sa.Column("run_id", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "phase IN ('retrieval','react','report','memory','system')",
            name="ck_run_events_phase",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_run_events_sequence"),
        sa.UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_run_events_run_sequence",
        ),
    )
    op.create_index(
        "ix_run_events_run_sequence",
        "run_events",
        ["run_id", "sequence"],
    )


def downgrade() -> None:
    """按 event → run → session 反向删除资源表，不影响知识图和长期记忆。

    回退会丢失诊断运行历史，只适用于开发/测试；先删依赖表可满足 PostgreSQL 外键顺序，早期迁移
    仍保留 case_memories、memory_evidence 和 GraphRAG 数据。
    """

    # 外键级联用于删除数据，不替代 DDL 依赖顺序；显式反序使迁移行为清晰可审计。
    op.drop_table("run_events")
    op.drop_table("agent_runs")
    op.drop_table("diagnosis_sessions")
