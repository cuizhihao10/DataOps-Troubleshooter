"""Planner ReAct 结构化决策契约。

本模块只描述可公开的决策摘要、假设更新和一批可并行的 Action。跨字段校验确保 call_tool、
finish 与 need_user_input 的字段组合合法，任何自由文本都不能绕过 Schema 直接驱动工具。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.tooling import McpToolRequest, ToolName

# 一轮最多并行三个只读 Action。上限存在的理由是成本而不是美观：每个 MCP 调用都会拉起一个独立
# stdio 子进程，无界并行会把"省下的等待"换成子进程风暴；三个恰好覆盖"状态 + 日志 + 拓扑"这类
# 真实互不依赖的取证组合，再多的组合在九个工具的边界内已经属于猜测式广撒网。
MAX_PARALLEL_TOOL_ACTIONS = 3


class PlannerStatus(StrEnum):
    """限定 Planner 每轮只能调用工具、结束调查或请求用户补充信息。

    三态枚举为 ReAct 循环提供确定性路由，不允许模型输出自由文本动作；具体字段组合还由
    `PlannerDecision` 的跨字段校验保证，避免状态与 Action 相互矛盾。
    """

    CALL_TOOL = "call_tool"
    FINISH = "finish"
    NEED_USER_INPUT = "need_user_input"


class HypothesisUpdateStatus(StrEnum):
    """描述一轮 Observation 对候选假设造成的新建、增强、削弱或拒绝影响。

    使用有限状态让公开决策摘要可被审计和评测，又不记录模型原始推理过程；每次更新可附带
    evidence_refs，以便后续检查状态变化是否真正由观察支持。
    """

    NEW = "new"
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    REJECTED = "rejected"


class PlannerStopReason(StrEnum):
    """限定 Planner 主动结束或请求补参时可以给出的七种可评测停止原因。

    这里必须是枚举而不是自由文本：停止原因会进入 `run_events`、`run-trace:v1` span 和 Golden
    评测的 `stop_reason_hit`，自由文本会同时造成三件事——公开事件里出现接近思维链的长篇叙述、
    span 属性白名单直接拒绝非 ASCII 值、以及"期望 evidence_sufficient"的评测项恒不命中。首次
    真实模型冒烟评测三个案例的 `stop_reason_hit_rate` 就是这样实测为 0：模型给出的是一整段
    中文理由，而不是可比较的分类。解释性文字继续写在 `decision_summary` 里，两者职责分离。
    """

    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    EVIDENCE_CONFLICT_REQUIRES_MANUAL_REVIEW = "evidence_conflict_requires_manual_review"
    TOOL_UNAVAILABLE_DEGRADED = "tool_unavailable_degraded"
    PERMISSION_DENIED_REQUIRES_ACCESS = "permission_denied_requires_access"
    MISSING_RESOURCE_ID = "missing_resource_id"
    NEED_USER_INPUT = "need_user_input"


class HypothesisUpdate(BaseModel):
    """保存 Planner 对单个假设的结构化状态更新、可选新建内容及证据引用。

    该对象只记录可公开的决策结果，不保存逐步 Reason；稳定 hypothesis_id 连接 AgentState 中的
    完整假设，引用列表允许确定性节点验证证据存在性。`status=new` 必须同时给出症状与候选根因，
    否则控制器无法把它投影成 `FaultHypothesis`——这正是首次真实模型评测里假设集合恒为空、
    确定性草稿因此永远拿不到根因的原因：契约只能"更新"假设，却没有任何字段能"创建"假设。
    组件范围刻意不放在这里，由能力路由的已批准组件决定，避免模型自述把未获批组件写进结论。
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=100)
    status: HypothesisUpdateStatus
    symptom: str | None = Field(default=None, max_length=1000)
    candidate_root_cause: str | None = Field(default=None, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_new_hypothesis_content(self) -> HypothesisUpdate:
        """要求 `new` 携带非空症状与候选根因，其余状态只引用既有假设。

        新建缺内容会让投影无法构造合法 `FaultHypothesis`，静默丢弃则会重现"模型说了根因、报告
        里没有根因"的结构性缺陷，因此在进入控制器前就失败。增强/削弱/拒绝允许省略这两个字段，
        因为内容已经存在于状态；给出空白字符串同样按缺失处理，避免用空格绕过校验。
        """

        symptom = (self.symptom or "").strip()
        root_cause = (self.candidate_root_cause or "").strip()
        if self.status is HypothesisUpdateStatus.NEW and not (symptom and root_cause):
            raise ValueError("new hypothesis updates require symptom and candidate_root_cause")
        return self


class ToolAction(BaseModel):
    """表示 Planner 选择的一个白名单 MCP 工具及已校验统一参数。

    工具名必须来自九项固定枚举，参数必须先通过 McpToolRequest；因此执行节点无需解析自然语言
    或接受任意命令，模型也不能借由 Action 越过只读工具边界。
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: ToolName
    arguments: McpToolRequest


class PlannerDecision(BaseModel):
    """定义 Planner 单轮 ReAct 的公开摘要、假设变化、Action 批次与停止原因。

    模型每轮只能提交一批互不依赖的只读 Action，并用跨字段规则绑定状态：调用工具时批次非空且
    不能提前停止，结束或补参时批次必须为空并说明原因。该契约既隐藏原始思维链，也让工作流分支
    可验证、可重放并受预算控制。
    """

    model_config = ConfigDict(extra="forbid")

    status: PlannerStatus
    decision_summary: str = Field(min_length=1, max_length=500)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    actions: list[ToolAction] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    # 停止原因是枚举而不是自由文本：它会直接进入公开事件、span 属性和 Golden 评测的分类比较，
    # 自由文本既会泄漏接近思维链的长篇理由，也让"命中期望停止原因"这一项永远无法为真。
    stop_reason: PlannerStopReason | None = None

    @model_validator(mode="after")
    def validate_actions_and_stop_reason(self) -> PlannerDecision:
        """校验状态、Action 批次与停止原因构成唯一且无歧义的合法组合。

        `call_tool` 分支要求批次在 1..MAX_PARALLEL_TOOL_ACTIONS 之间并继续循环；其他分支禁止残留
        Action，且必须给出可公开停止原因。批次上限刻意写在校验器里而不是 `Field(max_length=...)`：
        Structured Outputs 的 strict Schema 不接受 `maxItems`，把上限留在校验器可以同时保住"模型
        必须遵守上限"和"Schema 能被兼容端点接受"两件事。任何矛盾组合在进入执行器前抛出校验错误，
        防止自然语言摘要驱动隐式行为。
        """

        # 先处理唯一会继续执行外部动作的分支，确保批次和 stop_reason 互斥。
        if self.status is PlannerStatus.CALL_TOOL:
            if not self.actions:
                raise ValueError("call_tool decisions require at least one action")
            if len(self.actions) > MAX_PARALLEL_TOOL_ACTIONS:
                raise ValueError(
                    f"call_tool decisions accept at most {MAX_PARALLEL_TOOL_ACTIONS} actions"
                )
            if self.stop_reason is not None:
                raise ValueError("call_tool decisions cannot include stop_reason")
            return self

        # 结束类分支不允许携带“顺便执行”的工具调用，以保持一次决策只有一种副作用。
        if self.actions:
            raise ValueError("non-call_tool decisions must not include actions")
        if not self.stop_reason:
            raise ValueError("finish and need_user_input decisions require stop_reason")
        return self
