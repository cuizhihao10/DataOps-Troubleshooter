"""为持久化 per-run 调用链新增 run_trace_spans 表。

进程内 ContextVar 指标只能回答"刚刚这次请求"，Worker 重启或多进程部署后即全部丢失，因此生产级
多 Agent 系统必须把 span 落库。span 与 run 终态写在同一事务里：要么两者都可见，要么都不可见，
避免出现"run 成功但 trace 缺失"这种事后无法解释的状态。表级 CheckConstraint 复刻遥测契约的关键
约束，让绕过应用层的手工写入同样无法产生负耗时、未知层级或自引用父指针的残树。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260716_0009"
down_revision = "20260716_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 span 表，并建立按 run 回放与按层级聚合两类查询所需的索引。

    parent_span_id 刻意不加自引用外键：span 按开始顺序落库，父 span 通常晚于子 span 结束，同一批
    INSERT 内的顺序无法保证满足外键；结构完整性由应用层契约与压实逻辑保证。两个索引分别服务
    `GET /runs/{run_id}/trace` 的顺序回放和 /metrics 的 kind+name 聚合。
    """

    op.create_table(
        "run_trace_spans",
        sa.Column("span_id", sa.String(length=100), primary_key=True),
        sa.Column("run_id", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        # 父指针只是同表内的字符串引用：加自引用外键会让"父晚于子提交"的正常批量写入直接失败。
        sa.Column("parent_span_id", sa.String(length=100), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # run 删除即连带删除 trace：一份没有 run 的 span 树无法解释，留着只会让表无界增长。
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "kind IN ('workflow','node','react_step','tool_call','retrieval',"
            "'model_call','persistence')",
            name="ck_run_trace_spans_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ok','error','cancelled')",
            name="ck_run_trace_spans_status",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_run_trace_spans_sequence"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_run_trace_spans_duration"),
        sa.CheckConstraint("ended_at >= started_at", name="ck_run_trace_spans_interval"),
        sa.CheckConstraint(
            "parent_span_id IS NULL OR parent_span_id <> span_id",
            name="ck_run_trace_spans_parent",
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_trace_spans_run_sequence"),
    )
    # 两个索引对应两类完全不同的读路径：按 run 顺序回放 trace，以及按 kind+name 聚合 /metrics 样本。
    op.create_index("ix_run_trace_spans_run_sequence", "run_trace_spans", ["run_id", "sequence"])
    op.create_index("ix_run_trace_spans_kind_name", "run_trace_spans", ["kind", "name"])


def downgrade() -> None:
    """删除 span 表及其两个索引，使迁移链可以回退到文档 RAG 版本。

    先删索引再删表虽然对 PostgreSQL 并非必需（drop_table 会连带删除），但显式写出可以让回退脚本
    与升级脚本一一对应，避免后续新增表达式索引时漏删。
    """

    op.drop_index("ix_run_trace_spans_kind_name", table_name="run_trace_spans")
    op.drop_index("ix_run_trace_spans_run_sequence", table_name="run_trace_spans")
    op.drop_table("run_trace_spans")
