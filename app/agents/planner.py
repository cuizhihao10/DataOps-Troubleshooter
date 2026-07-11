"""定义 Planner Agent 与确定性 ReAct 编排层之间的可替换协议。

本模块不绑定模型供应商，只规定每轮 Planner 获得的已校验上下文和必须返回的结构化决策。
后续 OpenAI-compatible 适配器实现该协议；当前 LangGraph 控制器无需依赖具体 SDK。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import CapabilitySelection
from app.domain.models import AgentState
from app.domain.planner import PlannerDecision


class PlannerTurnContext(BaseModel):
    """封装 Planner 单轮决策所需的状态、能力策略和剩余运行预算。

    `state` 只包含公开摘要、证据和工具事件，不包含 Thought；capabilities 是确定性注册表输出。
    剩余毫秒和最大步骤由控制器计算，模型不能自行扩大预算或替换可用工具范围。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AgentState
    capabilities: CapabilitySelection
    max_react_steps: int = Field(ge=1, le=20)
    remaining_time_ms: int = Field(ge=0, le=600_000)

    @model_validator(mode="after")
    def validate_state_matches_capabilities(self) -> PlannerTurnContext:
        """保证注入状态中的意图与活动能力确实来自本次选择结果。

        该检查阻止路由状态与 Prompt 策略漂移，例如状态声称单组件却注入跨组件工具。失败会在
        调用模型前产生 Pydantic ValidationError，因此模型不会看到自相矛盾的上下文。
        """

        expected_names = [name.value for name in self.capabilities.active_capabilities]
        if self.state.intent != self.capabilities.intent.value:
            raise ValueError("state intent must match the capability selection")
        if self.state.active_capabilities != expected_names:
            raise ValueError("state active_capabilities must match the capability selection")
        if self.state.react_step >= self.max_react_steps:
            raise ValueError("planner cannot run after the ReAct action budget is exhausted")
        return self


@runtime_checkable
class PlannerAgent(Protocol):
    """声明 Planner LLM 适配器必须实现的异步结构化决策接口。

    实现可以调用 OpenAI-compatible 服务或测试替身，但必须返回 `PlannerDecision`。协议不提供
    Observation 写入能力，因而 Planner 无法伪造工具结果或绕过确定性 MCP 执行节点。
    """

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """根据已校验上下文返回且只返回一个 Planner ReAct 决策。

        输入包含当前状态、能力策略和剩余预算；输出必须通过 PlannerDecision Schema。实现失败
        应抛出异常交给控制器总超时/模型错误边界，不得返回自由文本或虚构 Observation。
        """

        ...
