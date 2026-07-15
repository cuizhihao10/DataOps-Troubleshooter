"""把同步诊断资源升级为 PostgreSQL 可靠 Worker 队列。

迁移保留既有 run/result/events/checkpoint 数据，新增 queued 状态、可空 started_at、领取次数和
租约字段。旧版本遗留的 running 行不能安全判断是否仍在执行，因此升级时显式标记为失败，避免
两个进程同时继续同一个未知状态；新 Worker 通过 ``FOR UPDATE SKIP LOCKED`` 和部分唯一索引实现
同 session 串行及崩溃后租约恢复。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260716_0006"
down_revision = "20260716_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为现有 agent_runs 添加队列/租约列并重建状态约束和索引。

    先删除依赖新列语义的旧 check，再写入安全默认值和转换遗留 running，最后创建新约束；这个顺序
    保证迁移中间状态不会被 PostgreSQL 约束拒绝。数据转换不接触 result 内容，只公开记录一次迁移失败。
    """

    # 旧约束不认识 queued 和新增 lease 列，必须先移除才能进行原子数据转换。
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.drop_constraint("ck_agent_runs_terminal_payload", "agent_runs", type_="check")
    op.add_column(
        "agent_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("agent_runs", sa.Column("lease_owner", sa.String(length=100), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "agent_runs",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )

    # 所有历史终态保留为可读结果；旧 running 没有 owner/lease，
    # 继续执行会造成重复工具调用，故安全失败。
    op.execute(
        sa.text(
            "UPDATE agent_runs SET "
            "status='failed', error_code='legacy_worker_migration', "
            "error_message='旧版同步运行未迁移为可恢复 Worker 任务。', "
            "completed_at=COALESCE(completed_at, now()), updated_at=now(), "
            "attempt_count=GREATEST(attempt_count, 1) "
            "WHERE status='running'"
        )
    )
    op.execute(sa.text("UPDATE agent_runs SET attempt_count=1 WHERE attempt_count < 1"))

    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_check_constraint(
        "ck_agent_runs_terminal_payload",
        "agent_runs",
        "(status = 'queued' AND result IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND started_at IS NULL AND completed_at IS NULL "
        "AND attempt_count = 0 AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
        "(status = 'running' AND result IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND started_at IS NOT NULL AND completed_at IS NULL "
        "AND attempt_count >= 1 AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(status = 'completed' AND result IS NOT NULL AND error_code IS NULL "
        "AND error_message IS NULL AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND attempt_count >= 1 AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
        "(status = 'failed' AND result IS NULL AND error_code IS NOT NULL "
        "AND error_message IS NOT NULL AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND attempt_count >= 1 AND lease_owner IS NULL AND lease_expires_at IS NULL)",
    )

    # 队列查询按状态/创建时间排序；同 session 的活跃唯一约束阻止追问并发污染 checkpoint。
    op.create_index("ix_agent_runs_queue", "agent_runs", ["status", "created_at"])
    op.create_index(
        "uq_agent_runs_active_session",
        "agent_runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    """把 queued/running 先安全转 failed，再恢复同步版列和约束。

    回退不重新执行未知任务，也不伪造 completed；队列中尚未执行或租约中的工作都以公开迁移失败
    终态保存，随后才删除租约列和部分索引。该操作只适合开发/测试环境的显式版本回退。
    """

    op.drop_index("uq_agent_runs_active_session", table_name="agent_runs")
    op.drop_index("ix_agent_runs_queue", table_name="agent_runs")
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.drop_constraint("ck_agent_runs_terminal_payload", "agent_runs", type_="check")
    op.execute(
        sa.text(
            "UPDATE agent_runs SET "
            "status='failed', error_code='worker_migration_downgrade', "
            "error_message='队列任务因回退同步资源契约而停止。', "
            "started_at=COALESCE(started_at, created_at), "
            "completed_at=COALESCE(completed_at, now()), "
            "updated_at=now(), attempt_count=GREATEST(attempt_count, 1), "
            "lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status IN ('queued','running')"
        )
    )
    op.drop_column("agent_runs", "lease_expires_at")
    op.drop_column("agent_runs", "lease_owner")
    op.drop_column("agent_runs", "attempt_count")
    op.alter_column(
        "agent_runs",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('running','completed','failed')",
    )
    op.create_check_constraint(
        "ck_agent_runs_terminal_payload",
        "agent_runs",
        "(status = 'running' AND result IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND completed_at IS NULL) OR "
        "(status = 'completed' AND result IS NOT NULL AND error_code IS NULL "
        "AND error_message IS NULL AND completed_at IS NOT NULL) OR "
        "(status = 'failed' AND result IS NULL AND error_code IS NOT NULL "
        "AND error_message IS NOT NULL AND completed_at IS NOT NULL)",
    )
