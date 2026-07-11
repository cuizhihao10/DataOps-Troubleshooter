"""定义五项运行时 capability 共用的强类型、只读数据契约。

这些模型把 Prompt 片段、工具优先级、输入要求和输出规则变成可序列化数据，让 Planner
组合策略时无需导入具体实现。模型不提供执行钩子，因此 capability 不会演变成第三类 Agent。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import Component
from app.domain.tooling import ToolName

CAPABILITY_CONTRACT_ID = "runtime-capabilities:v1"


class CapabilityName(StrEnum):
    """限定产品基线批准的五项运行时领域能力名称。

    字符串枚举可稳定进入状态、Prompt、健康检查和评测记录；拒绝任意字符串能防止新能力绕过
    产品审查，也明确这里是固定策略集合而不是可动态安装的插件目录。
    """

    SINGLE_COMPONENT_DIAGNOSIS = "single_component_diagnosis"
    CROSS_COMPONENT_CHAIN_TRACING = "cross_component_chain_tracing"
    HISTORY_CASE_MATCHING = "history_case_matching"
    RISK_ASSESSMENT = "risk_assessment"
    STRUCTURED_REPORTING = "structured_reporting"


class DiagnosisIntent(StrEnum):
    """表示当前最小路由层支持的两类诊断调查意图。

    该枚举复用 Golden Case 已有字符串，避免 capability 注册表自行解析自然语言。历史案例是
    调查中的可选增强而非第三种诊断主流程，因此由独立 `HistoryTrigger` 控制。
    """

    SINGLE_COMPONENT_DIAGNOSIS = "single_component_diagnosis"
    CROSS_COMPONENT_DIAGNOSIS = "cross_component_diagnosis"


class HistoryTrigger(StrEnum):
    """限定历史案例匹配可被加入本轮上下文的四种触发状态。

    `not_requested` 是安全默认值；其余三项逐一对应产品文档允许的用户请求、Planner 先例验证
    和可复用签名场景。显式来源便于事件审计，也防止每轮 ReAct 无条件召回旧案例。
    """

    NOT_REQUESTED = "not_requested"
    USER_REQUESTED = "user_requested"
    PLANNER_VALIDATION = "planner_validation"
    REUSABLE_SIGNATURE = "reusable_signature"


class CapabilityInputField(StrEnum):
    """枚举 capability 声明但不自行读取的结构化输入类别。

    输入名描述上游工作流必须提供的数据，不绑定具体存储或函数参数。注册表合并后，编排层可
    在调用 Planner 前统一检查缺口，而不是让 Prompt 在运行中猜测缺失字段。
    """

    USER_QUERY = "user_query"
    COMPONENTS = "components"
    RESOURCE_ID = "resource_id"
    TIME_RANGE = "time_range"
    SCENARIO_ID = "scenario_id"
    TRACE_ID = "trace_id"
    CURRENT_HYPOTHESES = "current_hypotheses"
    REALTIME_OBSERVATIONS = "realtime_observations"
    RETRIEVED_PATHS = "retrieved_paths"
    CONFIRMED_CASES = "confirmed_cases"


class CapabilityDefinition(BaseModel):
    """保存单项领域能力可交给 Planner 的完整静态策略定义。

    输入是名称、说明、Prompt、白名单工具顺序和校验规则，输出是冻结且禁止额外字段的配置。
    模型只承载数据，不含 LLM/MCP 回调；重复项或空策略会在模块导入/启动时立即校验失败。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: CapabilityName
    summary: str = Field(min_length=20, max_length=500)
    prompt_fragment: str = Field(min_length=80, max_length=5000)
    tool_priority: tuple[ToolName, ...] = ()
    required_inputs: tuple[CapabilityInputField, ...] = Field(min_length=1)
    output_validation_rules: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_policy_items(self) -> CapabilityDefinition:
        """拒绝工具、输入或输出规则中的重复项，保证合并行为可解释。

        注册表按声明顺序稳定去重，但单项定义内部的重复通常代表维护错误而非组合需要；本校验
        在定义创建时抛出 ValueError，使错误不能延迟到 Planner Prompt 已生成之后。
        """

        # 分别比较序列长度与集合长度，保留原始顺序的同时检测维护者误复制的配置项。
        if len(self.tool_priority) != len(set(self.tool_priority)):
            raise ValueError("capability tool_priority must not contain duplicates")
        if len(self.required_inputs) != len(set(self.required_inputs)):
            raise ValueError("capability required_inputs must not contain duplicates")
        if len(self.output_validation_rules) != len(set(self.output_validation_rules)):
            raise ValueError("capability output rules must not contain duplicates")
        return self


class CapabilitySelectionRequest(BaseModel):
    """描述确定性注册表选择能力所需的最小路由上下文。

    上游先产生类型化意图和组件范围，注册表只做组合，不解析自然语言。单组件范围必须恰好
    一个组件，跨组件范围至少两个；非法边界直接失败，避免错误工具集合进入 Planner。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: DiagnosisIntent
    components: tuple[Component, ...] = Field(min_length=1)
    history_trigger: HistoryTrigger = HistoryTrigger.NOT_REQUESTED

    @model_validator(mode="after")
    def validate_component_scope(self) -> CapabilitySelectionRequest:
        """校验意图与组件数量匹配，并拒绝重复组件。

        数量约束保证单组件工具过滤不会含糊，跨组件追踪也确实存在链路空间；重复组件没有信息
        增益且会污染审计输出，因此任一不变量破坏都通过 Pydantic ValidationError 暴露。
        """

        # 先拒绝重复值，否则两个相同组件可能错误满足跨组件的长度要求。
        if len(self.components) != len(set(self.components)):
            raise ValueError("components must not contain duplicates")
        if self.intent is DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS and len(self.components) != 1:
            raise ValueError("single-component diagnosis requires exactly one component")
        if self.intent is DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS and len(self.components) < 2:
            raise ValueError("cross-component diagnosis requires at least two components")
        return self


class CapabilitySelection(BaseModel):
    """表示一次可审计、可直接序列化进 Planner 上下文的能力组合。

    结果保留契约版本、路由输入、能力顺序、完整 Prompt 片段、工具顺序和合并后的输入/校验规则。
    模型被冻结并禁止额外字段，后续工作流可安全写入状态而不会被就地修改或混入执行对象。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(pattern=r"^runtime-capabilities:v\d+$")
    intent: DiagnosisIntent
    components: tuple[Component, ...]
    history_trigger: HistoryTrigger
    active_capabilities: tuple[CapabilityName, ...] = Field(min_length=3)
    prompt_fragments: tuple[str, ...] = Field(min_length=3)
    tool_priority: tuple[ToolName, ...] = Field(min_length=1)
    required_inputs: tuple[CapabilityInputField, ...] = Field(min_length=1)
    output_validation_rules: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aligned_fragments(self) -> CapabilitySelection:
        """保证每项活动能力恰好对应一个 Prompt 片段且不存在重复名称。

        位置对齐使审计者能把合并 Prompt 追溯到具体能力；重复能力会改变权重感知却没有业务
        含义，因此校验失败时拒绝生成上下文，而不是静默删除或继续执行。
        """

        if len(self.active_capabilities) != len(self.prompt_fragments):
            raise ValueError("each active capability requires exactly one prompt fragment")
        if len(self.active_capabilities) != len(set(self.active_capabilities)):
            raise ValueError("active capabilities must not contain duplicates")
        return self
