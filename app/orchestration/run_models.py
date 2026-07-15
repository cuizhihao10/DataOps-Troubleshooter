"""定义资源化诊断 API 的 session、message、run 和公开事件强类型契约。

这些模型位于编排层而不是 FastAPI 路由中，使 PostgreSQL 仓储、应用 runtime、HTTP 响应和测试共享
同一状态语义。结果只保存公开 LangGraph 事件和结构化报告，不包含模型原始思维链。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
)
from app.domain.models import Component
from app.orchestration.diagnosis_models import DiagnosisRunResult

DIAGNOSIS_API_CONTRACT_ID = "diagnosis-resources:v3"


class AgentRunStatus(StrEnum):
    """限定数据库队列 run 的等待、执行中、完成和失败四种持久化状态。

    POST 只创建 queued，Worker 领取后转 running，最终只能进入 completed 或 failed。枚举暂不声明
    cancelled，因为安全取消仍需独立协作协议；前端不能把浏览器 AbortController 冒充服务端取消。
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ActiveRunConflictError(RuntimeError):
    """表示同一 session 已有 queued/running run，新的消息不能并发入队。

    单 session 串行化保护 checkpoint 的单调版本和追问顺序；异常只携带公开 active run ID，API 可
    安全映射 409。它不包含 SQL、会话行、用户问题、模型输出或数据库连接信息。
    """

    def __init__(self, active_run_id: str) -> None:
        """保存可供客户端轮询的活跃 run ID，并构造不泄露内部状态的错误消息。

        ID 由已持久化行产生；调用方不应使用异常创建新 run 或自动重试，而应先读取该资源终态。
        """

        self.active_run_id = active_run_id
        super().__init__(f"diagnosis session already has active run: {active_run_id}")


class RunEventPhase(StrEnum):
    """标记公开 run event 属于检索、ReAct、报告、记忆还是系统失败阶段。

    phase 与 event_type 分开，客户端可按阶段分组而无需解析前缀；有限集合阻止数据库写入未审查
    的内部节点名或供应商日志类别。
    """

    RETRIEVAL = "retrieval"
    REACT = "react"
    REPORT = "report"
    MEMORY = "memory"
    SYSTEM = "system"


class DiagnosisSession(BaseModel):
    """表示一个可承载多次 message/run 的持久化排障会话。

    title 用于演示列表，last_user_query_summary 只保存截断公开摘要，不复制完整 Prompt。时间必须
    带时区且 updated 不早于 created，便于跨环境排序和未来 checkpoint 关联。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=r"^session_[a-f0-9]{16}$")
    title: str = Field(min_length=1, max_length=200)
    last_user_query_summary: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> DiagnosisSession:
        """校验会话时间可比较且更新时间没有倒退。

        naive datetime 会受服务器本地时区影响，因此在数据库/API 边界显式拒绝；更新时间倒退表示
        仓储转换或并发覆盖错误，不能静默纠正为当前时间。
        """

        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("diagnosis session timestamps must include a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("diagnosis session updated_at cannot precede created_at")
        return self


class DiagnosisMessage(BaseModel):
    """定义提交到一个 session 的用户消息和确定性 capability 路由输入。

    首版不依赖未实现的自然语言意图分类器，调用方显式提供 intent、组件和 history trigger；内容仍
    作为 Planner/GraphRAG 的不可信 user 数据。跨字段组件数量复用生产 capability 请求校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    intent: DiagnosisIntent
    components: tuple[Component, ...] = Field(min_length=1)
    history_trigger: HistoryTrigger = HistoryTrigger.NOT_REQUESTED

    @model_validator(mode="after")
    def validate_capability_route(self) -> DiagnosisMessage:
        """用 CapabilitySelectionRequest 验证意图、组件数量和重复组件边界。

        复用同一模型避免 API 与 LangGraph registry 形成两套规则；失败在创建 agent_run 前产生 422，
        因而无效消息不会留下孤立 running 记录或消耗检索/模型资源。
        """

        CapabilitySelectionRequest(
            intent=self.intent,
            components=self.components,
            history_trigger=self.history_trigger,
        )
        return self

    def capability_request(self) -> CapabilitySelectionRequest:
        """把已经校验的 API 消息投影为顶层 workflow 的 capability 请求。

        返回新冻结对象而不是共享可变字典；该转换不解析自然语言或改变 history trigger，因此 API
        审计记录与 Planner 实际路由输入保持一致。
        """

        return CapabilitySelectionRequest(
            intent=self.intent,
            components=self.components,
            history_trigger=self.history_trigger,
        )


class RunPublicEvent(BaseModel):
    """表示持久化并可由 `/events` 返回的一条安全运行时间线事件。

    summary 和 payload 只来自确定性投影；payload 可保存工具名、引用、审计码和记忆状态，但不能
    存放 Thought、模型原始输出、凭据或完整供应商异常。sequence 在单 run 内从一连续递增。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^run_evt_[a-f0-9]{16}$")
    run_id: str = Field(pattern=r"^run_[a-f0-9]{16}$")
    sequence: int = Field(ge=1)
    phase: RunEventPhase
    event_type: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def validate_timestamp(self) -> RunPublicEvent:
        """拒绝没有时区的事件时间，保证跨阶段排序不依赖服务器本地设置。

        sequence 是主要顺序，created_at 用于展示和耗时分析；时间非法时直接失败，仓储不能用 naive
        值伪装成 UTC。payload 内容的安全来源由事件投影函数和测试门禁保证。
        """

        if self.created_at.tzinfo is None:
            raise ValueError("run event created_at must include a timezone")
        return self


class AgentRunSnapshot(BaseModel):
    """保存数据库队列中一次诊断 run 的输入路由、进度、结果或安全失败信息。

    queued 尚无 started_at 且 attempt_count=0；running 已被 Worker 至少领取一次但不含结果/错误；
    completed 必须携带结果；failed 只保存稳定错误码和公开摘要。租约身份不进入公开快照。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^run_[a-f0-9]{16}$")
    session_id: str = Field(pattern=r"^session_[a-f0-9]{16}$")
    status: AgentRunStatus
    user_query: str = Field(min_length=1, max_length=4000)
    intent: DiagnosisIntent
    components: tuple[Component, ...] = Field(min_length=1)
    history_trigger: HistoryTrigger
    result: DiagnosisRunResult | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=100)
    error_message: str | None = Field(default=None, min_length=1, max_length=500)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime
    attempt_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_run_state(self) -> AgentRunSnapshot:
        """绑定 status 与结果/错误/完成时间，并校验 workflow 身份和时间单调性。

        queued 必须没有执行时间且尝试数为零；running/终态必须有 started_at 和正尝试数。completed
        结果必须与 run/session 相同；failed 不允许部分结果。所有存在的时间带时区并保持单调。
        """

        CapabilitySelectionRequest(
            intent=self.intent,
            components=self.components,
            history_trigger=self.history_trigger,
        )
        timestamps = [self.created_at, self.updated_at]
        if self.started_at is not None:
            timestamps.append(self.started_at)
        if self.completed_at is not None:
            timestamps.append(self.completed_at)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("agent run timestamps must include a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("agent run timestamps must be monotonic")
        if self.started_at is not None and not (
            self.created_at <= self.started_at <= self.updated_at
        ):
            raise ValueError("agent run timestamps must be monotonic")
        if (
            self.completed_at is not None
            and (
                self.started_at is None
                or not self.started_at <= self.completed_at <= self.updated_at
            )
        ):
            raise ValueError("agent run completion timestamp must be within run lifetime")

        if self.status is AgentRunStatus.QUEUED:
            if self.started_at is not None or self.attempt_count != 0:
                raise ValueError("queued run cannot be started or attempted")
            if any((self.result, self.error_code, self.error_message, self.completed_at)):
                raise ValueError("queued run cannot contain result, error, or completion time")
            return self
        if self.status is AgentRunStatus.RUNNING:
            if self.started_at is None or self.attempt_count < 1:
                raise ValueError("running run requires a start time and worker attempt")
            if (
                self.result is not None
                or self.error_code is not None
                or self.completed_at is not None
            ):
                raise ValueError("running run cannot contain result, error, or completion time")
            if self.error_message is not None:
                raise ValueError("running run cannot contain an error message")
            return self
        if self.completed_at is None:
            raise ValueError("terminal run requires completed_at")
        if self.started_at is None or self.attempt_count < 1:
            raise ValueError("terminal run requires a start time and worker attempt")
        if self.status is AgentRunStatus.COMPLETED:
            if self.result is None or self.error_code is not None or self.error_message is not None:
                raise ValueError("completed run requires result and no error")
            if self.result.react.state.run_id != self.run_id:
                raise ValueError("completed result run_id must match persisted run")
            if self.result.react.state.session_id != self.session_id:
                raise ValueError("completed result session_id must match persisted session")
        elif self.result is not None or self.error_code is None or self.error_message is None:
            raise ValueError("failed run requires error details and no result")
        return self


class ClaimedDiagnosisRun(BaseModel):
    """保存 Worker 领取后执行所需的公开 run 快照与私有租约身份。

    模型只在应用进程内部流转，不由 API 返回。worker_id 与 lease expiry 让完成、失败和心跳更新都能
    验证所有权；用户输入继续复用 AgentRunSnapshot，不复制为松散队列 payload。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: AgentRunSnapshot
    worker_id: str = Field(pattern=r"^worker_[a-f0-9]{16}$")
    lease_expires_at: datetime

    @model_validator(mode="after")
    def validate_claim(self) -> ClaimedDiagnosisRun:
        """要求领取结果处于 running，租约带时区且晚于本次状态更新时间。

        queued 或终态快照不能驱动 workflow；已过期/零长度租约也不能在进程内被误认为有效所有权。
        数据库仍以 owner 条件更新作为并发最终防线。
        """

        if self.run.status is not AgentRunStatus.RUNNING:
            raise ValueError("claimed diagnosis run must be running")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("claimed diagnosis lease must include a timezone")
        if self.lease_expires_at <= self.run.updated_at:
            raise ValueError("claimed diagnosis lease must expire after claim update")
        return self

    def message(self) -> DiagnosisMessage:
        """把持久化 run 输入恢复为 workflow 使用的冻结 DiagnosisMessage。

        转换不解析自然语言、不改变 history trigger，也不包含租约字段；Pydantic 会再次校验 capability
        组件边界，使损坏数据库行在模型或 MCP 调用前失败。
        """

        return DiagnosisMessage(
            content=self.run.user_query,
            intent=self.run.intent,
            components=self.run.components,
            history_trigger=self.run.history_trigger,
        )


class RunEventList(BaseModel):
    """封装一个 run 的连续公开事件列表和资源契约版本。

    空列表仅允许 running run 尚未写入首个事件的瞬间；非空列表必须 run_id 一致且 sequence 为
    1..N，防止 API 返回跨运行混合或缺口时间线。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(pattern=r"^diagnosis-resources:v\d+$")
    run_id: str = Field(pattern=r"^run_[a-f0-9]{16}$")
    events: tuple[RunPublicEvent, ...] = ()

    @model_validator(mode="after")
    def validate_event_sequence(self) -> RunEventList:
        """校验所有事件属于目标 run 且序号严格连续。

        仓储按 sequence 排序，但响应模型再次验证，避免 SQL 或测试替身漂移导致前端时间线错乱；
        失败显式暴露，不静默重排或丢弃重复事件。
        """

        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("run event list cannot mix run IDs")
        sequences = [event.sequence for event in self.events]
        if sequences and sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("run event sequence must be consecutive from one")
        return self
