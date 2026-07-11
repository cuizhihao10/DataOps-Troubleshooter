"""声明风险评估与结构化报告两项始终启用的输出策略。

它们为任何诊断意图补充安全和可审计性约束，不负责生成事实或执行修复。Planner 负责草稿，
Auditor 后续按相同规则复核，使建议风险、引用和不确定性拥有稳定的共享契约。
"""

from app.capabilities.base import (
    CapabilityDefinition,
    CapabilityInputField,
    CapabilityName,
)

RISK_ASSESSMENT = CapabilityDefinition(
    name=CapabilityName.RISK_ASSESSMENT,
    summary="为每项修复建议标注风险、前置条件、回滚和验证步骤，禁止 capability 直接执行写操作。",
    prompt_fragment="""对每项修复建议独立评估 low、medium 或 high 风险，并给出执行前置条件、
失败回滚和结果验证。建议只能描述人工可审查的处理方案，不能声称已经执行生产写操作；
高风险建议必须引用支持其必要性的证据。证据不足时优先提出只读检查或降低建议强度，
不得用确定语气掩盖不确定性。""",
    required_inputs=(
        CapabilityInputField.CURRENT_HYPOTHESES,
        CapabilityInputField.REALTIME_OBSERVATIONS,
        CapabilityInputField.RETRIEVED_PATHS,
    ),
    output_validation_rules=(
        "每项修复建议必须包含风险等级、前置条件、回滚说明和验证步骤。",
        "高风险建议必须至少关联一个支持其必要性的有效证据引用。",
        "所有建议仅描述人工操作计划，不得声称 capability 已执行生产写操作。",
    ),
)

STRUCTURED_REPORTING = CapabilityDefinition(
    name=CapabilityName.STRUCTURED_REPORTING,
    summary="把调查结果约束为可由 API、Auditor 和演示 UI 逐字段消费的结构化诊断报告。",
    prompt_fragment="""输出必须分别填写故障摘要、传播链、根因与置信度、证据引用、分步修复、
风险与回滚、证据不足项、后续检查和相似案例，不得用一段自由文本替代结构化字段。
每项根因与故障链关键边必须引用有效证据；无法确定时保留 uncertainties 并说明下一步，
而不是为了完整性编造结论。不要输出或保存原始 Thought。""",
    required_inputs=(
        CapabilityInputField.USER_QUERY,
        CapabilityInputField.CURRENT_HYPOTHESES,
        CapabilityInputField.REALTIME_OBSERVATIONS,
        CapabilityInputField.RETRIEVED_PATHS,
    ),
    output_validation_rules=(
        "报告必须保留摘要、链路、根因、证据、修复、风险、不确定性和相似案例的独立字段。",
        "每项根因和链路关键边必须引用当前证据集合中真实存在的 evidence_id 或 path_id。",
        "证据不足时必须输出 uncertainties 与下一步检查，且不得包含原始 Thought 或思维链。",
    ),
)
