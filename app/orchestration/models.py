"""定义有界 LangGraph ReAct 循环的输入、内部状态、事件和结果模型。

这些 Pydantic 契约把图节点之间的数据限制为可序列化对象，并把公开事件与原始模型推理隔离。
停止原因使用有限枚举，Planner 自主结束时则保留其通过 Schema 的公开原因字符串。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import CapabilitySelection, CapabilitySelectionRequest
from app.domain.models import AgentState, CaseMemory, MemoryStatus, SimilarCaseReference
from app.domain.tooling import ToolName
from app.retrieval.models import GraphEvidenceBundle

REACT_LOOP_CONTRACT_ID = "langgraph-react-loop:v2"


class ReactLoopStatus(StrEnum):
    """区分 LangGraph 循环仍可调度节点还是已经进入终态。

    只有 `running` 和 `stopped` 两态可使条件边保持简单、可穷举；具体结束原因单独保存，避免把
    每种失败扩张成新的控制流状态，也让健康检查和事件 API 使用稳定语义。
    """

    RUNNING = "running"
    STOPPED = "stopped"


class ReactStopReason(StrEnum):
    """列出确定性控制器能够主动产生的安全停止原因。

    Planner 的 finish/need_user_input 原因仍来自其结构化输出；本枚举只覆盖预算、总超时、重复
    Action、组件越界、trace 漂移和无效引用等控制器可以客观判定的失败路径。
    """

    REACT_BUDGET_EXHAUSTED = "react_budget_exhausted"
    TOTAL_TIMEOUT = "total_timeout"
    DUPLICATE_ACTION_BLOCKED = "duplicate_action_blocked"
    TOOL_NOT_ALLOWED_BY_CAPABILITY = "tool_not_allowed_by_capability"
    TRACE_ID_MISMATCH = "trace_id_mismatch"
    INVALID_EVIDENCE_REFERENCE = "invalid_evidence_reference"


class ReactEventType(StrEnum):
    """限定可以进入运行时间线的公开 ReAct 事件类别。

    事件只描述路由、Planner 决策摘要、Observation 摘要和停止，不包含 Thought。独立枚举让
    前端与评测无需从自然语言猜事件类型，也便于审计哪些 Action 被策略门禁拦截。
    """

    CAPABILITIES_SELECTED = "capabilities_selected"
    PLANNER_DECISION = "planner_decision"
    OBSERVATION_RECORDED = "observation_recorded"
    POLICY_BLOCKED = "policy_blocked"
    LOOP_STOPPED = "loop_stopped"


class ReactPublicEvent(BaseModel):
    """表示可安全写入日志、API 或评测时间线的一条结构化事件。

    事件包含稳定 ID、序号、简短摘要和可选工具/证据/停止原因，不复制完整响应或模型内部分析。
    POLICY_BLOCKED 与 LOOP_STOPPED 必须给出原因，确保任何终止都能由用户和测试解释。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^react_evt_[a-f0-9]{16}$")
    sequence: int = Field(ge=1)
    event_type: ReactEventType
    summary: str = Field(min_length=1, max_length=500)
    tool_name: ToolName | None = None
    observation_refs: tuple[str, ...] = ()
    stop_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_stop_event_reason(self) -> ReactPublicEvent:
        """校验终止类事件必须解释原因，普通事件则不能伪装成停止。

        先按事件类型判断是否需要 stop_reason，使事件消费者可仅凭类型安全展示终态。矛盾组合
        会在写入结果前抛出 ValidationError，不会留下“已停止但原因为空”的审计缺口。
        """

        stop_types = {ReactEventType.POLICY_BLOCKED, ReactEventType.LOOP_STOPPED}
        if self.event_type in stop_types and self.stop_reason is None:
            raise ValueError("stopping React events require stop_reason")
        if self.event_type not in stop_types and self.stop_reason is not None:
            raise ValueError("non-stopping React events cannot include stop_reason")
        return self


class ReactLoopConfig(BaseModel):
    """集中声明单次 Planner ReAct 运行的工具 Action 和墙钟预算。

    `max_steps` 统计 Planner 选择的工具 Action，不统计 MCP 内部重试；`total_timeout_seconds`
    覆盖路由、Planner、工具和图调度。模型冻结并限制范围，避免调用方在运行中扩大预算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=6, ge=1, le=20)
    total_timeout_seconds: float = Field(default=60, gt=0, le=600)


class ReactRunRequest(BaseModel):
    """封装一次有界循环的领域状态与确定性 capability 路由请求。

    调用方负责提供稳定 run/session ID、用户问题、意图和组件范围；控制器会覆盖状态中的旧路由
    字段，但保留既有证据和 ToolEvent 以支持未来会话恢复。额外字段被拒绝以暴露 API 漂移。
    """

    model_config = ConfigDict(extra="forbid")

    state: AgentState
    capability_request: CapabilitySelectionRequest
    evidence_bundle: GraphEvidenceBundle | None = None
    confirmed_case_memories: tuple[CaseMemory, ...] = ()
    history_case_matches: tuple[SimilarCaseReference, ...] = ()

    @model_validator(mode="after")
    def validate_confirmed_memories(self) -> ReactRunRequest:
        """拒绝把 pending/rejected 案例作为 Planner 已确认历史上下文。

        运行请求是长期记忆与 Planner 之间的确定性边界；任何非 confirmed 案例都会产生 Pydantic
        ValidationError，而不是依赖 Prompt 提醒模型忽略，从结构上降低记忆污染风险。
        """

        if any(
            memory.status is not MemoryStatus.CONFIRMED for memory in self.confirmed_case_memories
        ):
            raise ValueError("React runs can include only confirmed case memories")
        if [item.memory_id for item in self.confirmed_case_memories] != [
            item.case_id for item in self.history_case_matches
        ]:
            raise ValueError("React history explanations must match confirmed memory order")
        return self


class ReactGraphState(BaseModel):
    """保存 LangGraph 节点之间传递的完整、经过验证的循环状态。

    除领域 `AgentState` 外，还保存路由请求、能力选择、公开事件和已执行 Action 指纹。指纹阻止
    同参重复调用；状态不保存 Planner 原始 Reason，能够安全进入未来 checkpoint。
    """

    model_config = ConfigDict(extra="forbid")

    agent_state: AgentState
    capability_request: CapabilitySelectionRequest
    evidence_bundle: GraphEvidenceBundle | None = None
    confirmed_case_memories: tuple[CaseMemory, ...] = ()
    history_case_matches: tuple[SimilarCaseReference, ...] = ()
    capability_selection: CapabilitySelection | None = None
    events: list[ReactPublicEvent] = Field(default_factory=list)
    executed_action_fingerprints: list[str] = Field(default_factory=list)
    status: ReactLoopStatus = ReactLoopStatus.RUNNING


class ReactRunResult(BaseModel):
    """返回有界 ReAct 循环的最终状态、能力快照和公开事件时间线。

    成功返回必然处于 stopped 且带有 stop_reason；能力快照允许重放当时的 Prompt/工具边界，
    事件则供 API 展示 Action/Observation，不包含模型原始思维链或未验证自由文本字段。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(pattern=r"^langgraph-react-loop:v\d+$")
    state: AgentState
    capabilities: CapabilitySelection
    events: list[ReactPublicEvent] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_terminal_result(self) -> ReactRunResult:
        """确保图只在有公开停止原因和终止事件时返回结果。

        该校验防止 LangGraph 因错误边配置静默结束，也保证 API 不需要猜测运行是否仍在继续。
        如果缺少 stop_reason 或最后事件不是 LOOP_STOPPED/POLICY_BLOCKED，结果构造会失败。
        """

        if not self.state.stop_reason:
            raise ValueError("completed React runs require state.stop_reason")
        if self.events[-1].event_type not in {
            ReactEventType.LOOP_STOPPED,
            ReactEventType.POLICY_BLOCKED,
        }:
            raise ValueError("completed React runs require a terminal public event")
        return self
