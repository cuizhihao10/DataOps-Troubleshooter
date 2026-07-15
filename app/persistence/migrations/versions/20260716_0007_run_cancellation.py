"""为 diagnosis run 增加可审计的用户取消终态。

取消与失败不是同一种业务语义：失败表示系统无法完成任务，取消表示用户明确停止
任务。迁移通过先删除旧约束、再重建包含 ``cancelled`` 的状态/载荷约束完成兼容升级，
避免 PostgreSQL 在中间状态拒绝合法的数据转换。
"""

import sqlalchemy as sa
from alembic import op

revision = "20260716_0007"
down_revision = "20260716_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展 agent_runs 的状态约束，使 queued/running 可以安全进入 cancelled。

    迁移先移除旧约束再创建新约束，保证 PostgreSQL 在 DDL 中间状态不会拒绝合法
    取消行；不修改已有业务结果或事件，因而可重复验证审计数据。
    """

    # 先移除旧约束，随后一次性安装新语义；否则 PostgreSQL 会在 UPDATE 阶段先拦截取消行。
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.drop_constraint("ck_agent_runs_terminal_payload", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('queued','running','completed','failed','cancelled')",
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
        "AND attempt_count >= 1 AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
        "(status = 'cancelled' AND result IS NULL AND error_code IS NOT NULL "
        "AND error_message IS NOT NULL AND completed_at IS NOT NULL "
        "AND lease_owner IS NULL AND lease_expires_at IS NULL AND "
        "((started_at IS NULL AND attempt_count = 0) OR "
        "(started_at IS NOT NULL AND attempt_count >= 1)))",
    )


def downgrade() -> None:
    """将取消记录转换为可追踪的失败，再恢复旧版四状态约束。

    旧版 Schema 无法表达 cancelled，回退时显式保存迁移错误码和结束时间，避免
    删除用户操作记录或留下违反旧约束的 queued/running 行。
    """

    # 旧版本无法表达 cancelled；将其显式转换为失败而不是静默删除，保留审计语义。
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.drop_constraint("ck_agent_runs_terminal_payload", "agent_runs", type_="check")
    op.execute(
        sa.text(
            "UPDATE agent_runs SET status='failed', error_code='cancellation_migration_downgrade', "
            "error_message='取消状态因数据库版本回退被转换为失败。', "
            "started_at=COALESCE(started_at, created_at), "
            "completed_at=COALESCE(completed_at, now()), "
            "attempt_count=GREATEST(attempt_count, 1), updated_at=now(), "
            "lease_owner=NULL, lease_expires_at=NULL WHERE status='cancelled'"
        )
    )
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
