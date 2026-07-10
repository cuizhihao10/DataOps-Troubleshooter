"""Planner ReAct 结构化决策契约。

本模块只描述可公开的决策摘要、假设更新和单个 Action。跨字段校验确保 call_tool、finish
与 need_user_input 的字段组合合法，任何自由文本都不能绕过 Schema 直接驱动工具。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.tooling import McpToolRequest, ToolName


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


class HypothesisUpdate(BaseModel):
    """保存 Planner 对单个假设的结构化状态更新及证据引用。

    该对象只记录可公开的决策结果，不保存逐步 Reason；稳定 hypothesis_id 连接 AgentState 中的
    完整假设，引用列表允许确定性节点验证证据存在性。
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=100)
    status: HypothesisUpdateStatus
    evidence_refs: list[str] = Field(default_factory=list)


class ToolAction(BaseModel):
    """表示 Planner 选择的一个白名单 MCP 工具及已校验统一参数。

    工具名必须来自九项固定枚举，参数必须先通过 McpToolRequest；因此执行节点无需解析自然语言
    或接受任意命令，模型也不能借由 Action 越过只读工具边界。
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: ToolName
    arguments: McpToolRequest


class PlannerDecision(BaseModel):
    """定义 Planner 单轮 ReAct 的公开摘要、假设变化、Action 与停止原因。

    模型只允许一个结构化 Action，并用跨字段规则绑定状态：调用工具时必须有 Action 且不能提前
    停止，结束或补参时必须没有 Action 且说明原因。该契约既隐藏原始思维链，也让工作流分支
    可验证、可重放并受预算控制。
    """

    model_config = ConfigDict(extra="forbid")

    status: PlannerStatus
    decision_summary: str = Field(min_length=1, max_length=500)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    action: ToolAction | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    stop_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_action_and_stop_reason(self) -> PlannerDecision:
        """校验状态、Action 与停止原因构成唯一且无歧义的合法组合。

        `call_tool` 分支要求执行信息完整并继续循环；其他分支禁止残留 Action，且必须给出可公开
        停止原因。任何矛盾组合在进入执行器前抛出校验错误，防止自然语言摘要驱动隐式行为。
        """

        # 先处理唯一会继续执行外部动作的分支，确保 Action 和 stop_reason 互斥。
        if self.status is PlannerStatus.CALL_TOOL:
            if self.action is None:
                raise ValueError("call_tool decisions require an action")
            if self.stop_reason is not None:
                raise ValueError("call_tool decisions cannot include stop_reason")
            return self

        # 结束类分支不允许携带“顺便执行”的工具调用，以保持一次决策只有一种副作用。
        if self.action is not None:
            raise ValueError("non-call_tool decisions must not include an action")
        if not self.stop_reason:
            raise ValueError("finish and need_user_input decisions require stop_reason")
        return self
