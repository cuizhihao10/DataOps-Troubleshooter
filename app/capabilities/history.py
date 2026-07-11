"""声明按需匹配已确认历史案例的运行时策略。

本模块只定义召回输入、比较输出和证据优先级；真正的案例查询将在长期记忆服务切片实现。
将策略先固定为数据契约，可防止后续把未确认案例或旧结论直接混入实时诊断。
"""

from app.capabilities.base import (
    CapabilityDefinition,
    CapabilityInputField,
    CapabilityName,
)

HISTORY_CASE_MATCHING = CapabilityDefinition(
    name=CapabilityName.HISTORY_CASE_MATCHING,
    summary="按显式触发条件比较已确认案例，输出相似点、差异点、历史方案、避坑提示和证据引用。",
    prompt_fragment="""只使用 confirmed 的历史案例，并保留案例 ID、确认状态、相似度和引用。
逐项比较本次组件、症状、候选根因、故障路径和关键 Observation，分别列出共同点与差异点；
历史方案只能作为参考和风险提示，不能直接确认本次根因。任何历史记录与实时 Observation
冲突时，必须突出差异并以本次实时证据为准。""",
    required_inputs=(
        CapabilityInputField.COMPONENTS,
        CapabilityInputField.CURRENT_HYPOTHESES,
        CapabilityInputField.REALTIME_OBSERVATIONS,
        CapabilityInputField.RETRIEVED_PATHS,
        CapabilityInputField.CONFIRMED_CASES,
    ),
    output_validation_rules=(
        "默认召回结果只能包含 confirmed 案例，并保留案例 ID、确认状态、相似度和证据引用。",
        "每个命中必须分别给出共同点、差异点、参考方案和避坑提示。",
        "历史案例与本次实时 Observation 冲突时必须突出差异并以实时 Observation 为准。",
    ),
)
