"""为方案类知识节点增加人工声明的处置风险等级列与范围约束。

Revision ID: 20260716_0010
Revises: 20260716_0009
Create Date: 2026-07-16

报告层此前把所有知识方案硬编码为 medium，`RiskLevel.HIGH` 在生产路径上不可达，于是"是否需要
审批与回滚演练"这一控制语义实际由代码常量而不是知识决定。本迁移把风险等级下沉到知识节点，
并用 CheckConstraint 保证它当且仅当出现在 solution/sop 行上。
"""

import sqlalchemy as sa
from alembic import op

revision = "20260716_0010"
down_revision = "20260716_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空 remediation_risk_level 列，并约束其取值与所属节点类型。

    列先以可空形式加入，使已有 47/54 个非方案节点无需回填即可完成迁移；随后的 CheckConstraint
    双向收紧：solution/sop 必须落在 low/medium/high 之一，其他类型必须为 NULL。之所以不做成
    带默认值的非空列，是因为"默认 medium"正是本次要消除的那个静默降级——缺声明必须报错。
    """

    op.add_column(
        "knowledge_nodes",
        sa.Column("remediation_risk_level", sa.String(length=10), nullable=True),
    )

    # 先回填当前种子里唯一需要声明的类型，再建约束：反序会让既有 solution 行直接违约。
    op.execute(
        "UPDATE knowledge_nodes SET remediation_risk_level = 'medium' "
        "WHERE node_type IN ('solution', 'sop') AND remediation_risk_level IS NULL"
    )

    op.create_check_constraint(
        "ck_knowledge_nodes_remediation_risk_level",
        "knowledge_nodes",
        "(node_type IN ('solution','sop') AND remediation_risk_level IN "
        "('low','medium','high')) OR "
        "(node_type NOT IN ('solution','sop') AND remediation_risk_level IS NULL)",
    )


def downgrade() -> None:
    """先删除范围约束再删除列，回退后报告风险等级重新退回代码常量。

    回退会丢失人工声明的风险等级，因此只应用于开发测试：一旦该列消失，高风险处置建议就无法
    与中风险区分，报告仍会生成建议但不再要求审批与回滚演练。
    """

    op.drop_constraint(
        "ck_knowledge_nodes_remediation_risk_level",
        "knowledge_nodes",
        type_="check",
    )
    op.drop_column("knowledge_nodes", "remediation_risk_level")
