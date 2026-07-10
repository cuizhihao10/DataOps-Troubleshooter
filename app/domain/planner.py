"""Planner ReAct 结构化决策契约。

本模块只描述可公开的决策摘要、假设更新和单个 Action。跨字段校验确保 call_tool、finish
与 need_user_input 的字段组合合法，任何自由文本都不能绕过 Schema 直接驱动工具。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.tooling import McpToolRequest, ToolName


class PlannerStatus(StrEnum):
    CALL_TOOL = "call_tool"
    FINISH = "finish"
    NEED_USER_INPUT = "need_user_input"


class HypothesisUpdateStatus(StrEnum):
    NEW = "new"
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    REJECTED = "rejected"


class HypothesisUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=100)
    status: HypothesisUpdateStatus
    evidence_refs: list[str] = Field(default_factory=list)


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: ToolName
    arguments: McpToolRequest


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PlannerStatus
    decision_summary: str = Field(min_length=1, max_length=500)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    action: ToolAction | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    stop_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_action_and_stop_reason(self) -> PlannerDecision:
        if self.status is PlannerStatus.CALL_TOOL:
            if self.action is None:
                raise ValueError("call_tool decisions require an action")
            if self.stop_reason is not None:
                raise ValueError("call_tool decisions cannot include stop_reason")
            return self

        if self.action is not None:
            raise ValueError("non-call_tool decisions must not include an action")
        if not self.stop_reason:
            raise ValueError("finish and need_user_input decisions require stop_reason")
        return self
