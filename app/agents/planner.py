"""定义 Planner Agent 与确定性 ReAct 编排层之间的可替换协议。

本模块不绑定模型供应商，只规定每轮 Planner 获得的已校验上下文、必须返回的结构化决策和
可公开失败。OpenAI-compatible 实现在相邻模块中依赖这些契约，LangGraph 无需依赖具体 SDK。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import CapabilitySelection
from app.domain.models import AgentState, CaseMemory, MemoryStatus, SimilarCaseReference
from app.domain.planner import MAX_PARALLEL_TOOL_ACTIONS, PlannerDecision
from app.retrieval.models import GraphEvidenceBundle


class PlannerAgentError(RuntimeError):
    """表示 Planner 适配层可安全映射为公开停止原因的预期失败。

    异常只保存受控 summary 和 stop_reason，不把 API key、完整响应体或底层堆栈写入状态；
    LangGraph 仅捕获该基类，未预期编程错误仍会传播以便及时修复。
    """

    def __init__(self, *, stop_reason: str, public_summary: str) -> None:
        """初始化可公开停止原因与摘要，并保持标准 RuntimeError 行为。

        两个输入都由适配层预先净化；`str(error)` 只返回 public_summary，调用者无需解析供应商
        异常文本。空值由具体子类构造逻辑避免，不在此静默补默认信息。
        """

        super().__init__(public_summary)
        self.stop_reason = stop_reason
        self.public_summary = public_summary


class PlannerOutputValidationError(PlannerAgentError):
    """表示模型输出未通过 JSON/Pydantic 校验，可能触发一次受控修复。

    raw_output 仅在当前调用内用于修复 Prompt，不应写入 AgentState、日志或 API；异常字符串仍只
    暴露安全摘要。attempts 记录已经失败的生成次数，第二次失败后控制器停止。
    """

    def __init__(
        self,
        *,
        validation_summary: str,
        raw_output: str,
        attempts: int = 1,
    ) -> None:
        """保存截断原输出、校验摘要和失败次数，同时设置稳定停止原因。

        调用方负责将 raw_output 限制在修复上下文预算内；attempts 只能表达一次或两次生成失败。
        异常不会把 raw_output 传给 RuntimeError，防止通用日志中意外记录模型文本。
        """

        if attempts not in {1, 2}:
            raise ValueError("Planner output attempts must be 1 or 2")
        super().__init__(
            stop_reason="planner_output_invalid",
            public_summary=(f"Planner 结构化输出在 {attempts} 次生成后仍未通过 Schema 校验。"),
        )
        self.validation_summary = validation_summary[:2000]
        self.raw_output = raw_output[:8000]
        self.attempts = attempts


class PlannerRefusalError(PlannerAgentError):
    """表示模型通过结构化 refusal 字段拒绝当前请求。

    拒绝不属于格式错误，因此不能用修复 Prompt 反复规避安全判断；异常只公开“模型拒绝”事实，
    原始 refusal 文本被截断保存在属性中供受控诊断，不写入运行状态。
    """

    def __init__(self, refusal: str) -> None:
        """保存截断 refusal，并设置稳定的 planner_refusal 停止原因。

        原始文本可能包含供应商策略细节，因此 RuntimeError 消息不直接复制它；空 refusal 使用
        通用占位语义，保证异常仍可审计而不会伪造具体拒绝原因。
        """

        super().__init__(
            stop_reason="planner_refusal",
            public_summary="Planner 模型拒绝生成本轮结构化决策。",
        )
        self.refusal = (refusal or "unspecified refusal")[:2000]


class PlannerProviderError(PlannerAgentError):
    """表示 OpenAI-compatible 传输、认证、限流或服务端失败。

    error_code 提供供应商无关分类；retryable 标记 429/5xx、超时与连接失败，认证失败被显式排除。
    Provider 自身仍然一次 complete 只发一次请求，重试由 `RetryingPlannerChatProvider` 在外层按
    有界预算与指数退避执行，因此每次尝试各自留下独立遥测。公开摘要不包含 URL 查询、响应体或凭据。
    """

    def __init__(
        self,
        *,
        error_code: str,
        public_summary: str,
        retryable: bool,
    ) -> None:
        """初始化稳定分类、可公开摘要和是否值得稍后重试的标记。

        所有供应商异常先在 Provider 边界映射后才进入本对象；`retryable` 由重试包装层消费，预算
        耗尽或标记为假时原样上抛，让 ReAct 循环照旧以 planner_provider_error 收口。
        """

        super().__init__(
            stop_reason="planner_provider_error",
            public_summary=public_summary,
        )
        self.error_code = error_code
        self.retryable = retryable


class PlannerTurnContext(BaseModel):
    """封装 Planner 单轮决策所需的状态、能力策略和剩余运行预算。

    `state` 只包含公开摘要、证据和工具事件，不包含 Thought；capabilities 是确定性注册表输出。
    剩余毫秒、最大步骤和本轮可并行 Action 数由控制器计算，模型不能自行扩大预算、提高并行度或
    替换可用工具范围。`closing_turn` 标记取证预算耗尽后那一次只允许 finish 的收口回合，它与
    `max_parallel_actions == 0` 必须同时成立，避免向模型注入自相矛盾的预算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AgentState
    capabilities: CapabilitySelection
    evidence_bundle: GraphEvidenceBundle | None = None
    confirmed_case_memories: tuple[CaseMemory, ...] = ()
    history_case_matches: tuple[SimilarCaseReference, ...] = ()
    max_react_steps: int = Field(ge=1, le=20)
    # 下界是 0 而不是 1：收口回合把"本轮不允许任何工具调用"直接编码成批次上限 0，让同一个字段既
    # 表达并行度也表达"取证已关闭"，模型无需从两处配置自己推断。
    max_parallel_actions: int = Field(ge=0, le=MAX_PARALLEL_TOOL_ACTIONS)
    closing_turn: bool = False
    remaining_time_ms: int = Field(ge=0, le=600_000)

    @model_validator(mode="after")
    def validate_state_matches_capabilities(self) -> PlannerTurnContext:
        """保证注入状态中的意图、活动能力与并行上限确实来自本次选择和剩余预算。

        该检查阻止路由状态与 Prompt 策略漂移，例如状态声称单组件却注入跨组件工具，或者在只剩一个
        工具步数时仍告诉模型可以并行三个。失败会在调用模型前产生 Pydantic ValidationError，因此
        模型不会看到自相矛盾的上下文。
        """

        expected_names = [name.value for name in self.capabilities.active_capabilities]
        if self.state.intent != self.capabilities.intent.value:
            raise ValueError("state intent must match the capability selection")
        if self.state.active_capabilities != expected_names:
            raise ValueError("state active_capabilities must match the capability selection")
        # closing_turn 与批次上限 0 互为冗余表达，两侧必须同时成立：只写一侧就会出现"告诉模型这是
        # 最后一轮、却仍允许它提交工具调用"或者反过来"批次上限 0 但没说明原因"的矛盾上下文，而模型
        # 面对矛盾预算的行为无法预测。控制器写错哪一侧都在调用模型之前失败。
        if self.closing_turn != (self.max_parallel_actions == 0):
            raise ValueError("closing turn must coincide with a zero parallel action allowance")
        if any(
            memory.status is not MemoryStatus.CONFIRMED for memory in self.confirmed_case_memories
        ):
            raise ValueError("Planner context can include only confirmed case memories")
        if [item.memory_id for item in self.confirmed_case_memories] != [
            item.case_id for item in self.history_case_matches
        ]:
            raise ValueError("Planner history explanations must match confirmed memory order")
        # 以下两条只在取证回合成立。收口回合是预算耗尽后专门发放的额度，react_step 此时必然已经等于
        # max_react_steps，它唯一的用途就是让模型把结论写进 hypothesis_updates；上面与预算无关的
        # 校验对两种回合一律适用，不能因为放行收口回合而顺带跳过。
        if self.closing_turn:
            return self
        if self.state.react_step >= self.max_react_steps:
            raise ValueError("planner cannot run after the ReAct action budget is exhausted")
        if self.max_parallel_actions > self.max_react_steps - self.state.react_step:
            raise ValueError("parallel action allowance cannot exceed the remaining step budget")
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
