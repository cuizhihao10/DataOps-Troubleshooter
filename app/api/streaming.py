"""定义 run-stream:v1 的 SSE 帧契约与增量推流生成器。

推流刻意不是"另一条执行路径"：run 仍由 PostgreSQL Worker 执行，本模块只把已经落库的 run 状态和
公开事件按游标增量推给客户端，因此推流断开、超时或客户端不支持 SSE 都不会改变任何 run 的结果，
前端可以随时退回轮询。帧内容与 `/events` 完全同源，不含 Thought、Prompt、embedding 或凭据。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import StrEnum
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sse_starlette import ServerSentEvent

from app.orchestration.run_models import (
    AgentRunSnapshot,
    AgentRunStatus,
    RunPublicEvent,
)

RUN_STREAM_CONTRACT_ID = "run-stream:v1"

TERMINAL_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }
)


class RunStreamFrameKind(StrEnum):
    """限定 SSE 帧只能是状态快照、单条公开事件或流结束通知三种。

    帧类型是封闭枚举而不是自由字符串，客户端可以只实现三个分支；新增类型必须提升契约版本，
    避免前端在未知帧上静默丢数据。
    """

    RUN_SNAPSHOT = "run_snapshot"
    RUN_EVENT = "run_event"
    STREAM_END = "stream_end"


class RunStreamEndReason(StrEnum):
    """限定流结束原因，区分"run 真的结束了"与"只是这条连接结束了"。

    三种终态原因表示 run 已经不会再产生事件，客户端应停止重连；`stream_timeout` 与
    `run_disappeared` 表示连接侧原因，客户端应带游标重连或退回轮询。混用这两类会让演示者把
    "连接超时"误读成"诊断失败"。
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STREAM_TIMEOUT = "stream_timeout"
    RUN_DISAPPEARED = "run_disappeared"


class RunStreamFrame(BaseModel):
    """表示一条可安全推送的 SSE 帧，携带游标、run 状态和可选公开事件。

    `cursor` 是客户端断线重连时应回传的最后事件序号，因此每一帧都必须自带游标而不是让客户端
    自己累加。事件载荷直接复用 `RunPublicEvent`，所以推流与 `/events` 不可能出现两套字段语义。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(pattern=r"^run-stream:v\d+$")
    kind: RunStreamFrameKind
    run_id: str = Field(pattern=r"^run_[a-f0-9]{16}$")
    cursor: int = Field(ge=0)
    status: AgentRunStatus
    event: RunPublicEvent | None = None
    end_reason: RunStreamEndReason | None = None

    @model_validator(mode="after")
    def validate_frame_payload(self) -> RunStreamFrame:
        """绑定帧类型与可选字段，保证客户端不需要防御性判空。

        事件帧必须携带同 run 且序号等于游标的事件；结束帧必须携带原因；快照帧两者都不允许。
        任何组合错误在推送前失败，而不是让前端渲染出一条空事件。
        """

        if self.kind is RunStreamFrameKind.RUN_EVENT:
            if self.event is None:
                raise ValueError("run_event frame requires an event payload")
            if self.event.run_id != self.run_id:
                raise ValueError("run_event frame cannot mix run IDs")
            if self.event.sequence != self.cursor:
                raise ValueError("run_event frame cursor must equal the event sequence")
        elif self.event is not None:
            raise ValueError("only run_event frames may carry an event payload")
        if self.kind is RunStreamFrameKind.STREAM_END:
            if self.end_reason is None:
                raise ValueError("stream_end frame requires an end reason")
        elif self.end_reason is not None:
            raise ValueError("only stream_end frames may carry an end reason")
        return self

    def to_server_sent_event(self) -> ServerSentEvent:
        """把强类型帧编码为命名 SSE 事件，并把游标写入标准 `id` 字段。

        用 `id` 而不是自定义字段承载游标，浏览器 `EventSource` 断线重连时会自动带上
        `Last-Event-ID`，服务端因此无需额外协议就能续传；`event` 名让客户端按帧类型分发。
        """

        return ServerSentEvent(
            data=self.model_dump_json(exclude_none=True),
            event=self.kind.value,
            id=str(self.cursor),
        )


class RunStreamConfig(BaseModel):
    """集中配置推流的轮询间隔、心跳周期和单连接最长存活时间。

    三个值互相约束：轮询间隔决定推送延迟下限，心跳周期防止反向代理掐掉空闲连接，最长存活时间
    则保证一条被遗忘的连接不会永久占用数据库会话。默认值面向本地演示而不是公网高并发。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    poll_seconds: float = Field(default=0.5, gt=0, le=10)
    keepalive_seconds: float = Field(default=15, gt=0, le=120)
    max_seconds: float = Field(default=300, gt=1, le=3600)

    @model_validator(mode="after")
    def validate_stream_budget(self) -> RunStreamConfig:
        """要求轮询快于心跳、心跳短于连接寿命，否则配置无法达到它声称的效果。

        轮询慢于心跳时客户端会先收到心跳再收到早已落库的事件，"实时"就名不副实；心跳不短于
        连接寿命时这条连接根本等不到第一次心跳，代理仍会按空闲超时掐断。
        """

        if self.poll_seconds >= self.keepalive_seconds:
            raise ValueError("run stream poll interval must be shorter than the keepalive period")
        if self.keepalive_seconds >= self.max_seconds:
            raise ValueError("run stream keepalive period must be shorter than the max lifetime")
        return self


class RunStreamSource(Protocol):
    """声明推流只需要的两个只读查询，阻止生成器获得执行或写入能力。

    生成器拿不到 workflow、仓储事务或 Worker 接口，因此一条 SSE 连接在结构上无法推进、重试或
    修改 run；`DiagnosisApplicationRuntime` 天然满足该协议，测试也可以注入确定性替身。
    """

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """返回 run 当前持久化快照，未知或已清理的 run 返回 None 而不是抛错。

        推流每一跳都要重新读快照，因此实现必须是短会话只读查询：长事务里的快照读看不到
        Worker 后续提交的状态，会让连接永远停在 running。
        """

    async def get_events_after(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> tuple[RunPublicEvent, ...] | None:
        """返回序号严格大于游标的公开事件元组，未知 run 返回 None 以区别于"暂无新事件"。

        返回的是增量切片而不是完整时间线，因此实现应把过滤下推到 SQL；元组为空表示这一跳
        没有新事件，None 表示 run 已不存在，两者会驱动生成器走完全不同的收尾分支。
        """


async def iter_run_stream(
    source: RunStreamSource,
    run_id: str,
    *,
    after_sequence: int = 0,
    config: RunStreamConfig,
) -> AsyncIterator[ServerSentEvent]:
    """按游标增量产出该 run 的状态、事件与结束帧，直到 run 终态或连接预算耗尽。

    每轮先读 run 快照再读增量事件：终态 run 与它最后一批事件写在同一事务里，若顺序颠倒就可能
    先看到 completed、再读到尚未提交的事件，从而漏掉最后几条时间线。状态只在变化时推送，事件
    每条一帧，因此客户端可以只追加而不做去重。
    """

    deadline = monotonic() + config.max_seconds
    cursor = after_sequence
    last_status: AgentRunStatus | None = None
    while True:
        run = await source.get_run(run_id)
        if run is None:
            # run 在推流期间消失只可能是外部清理，客户端不该按"诊断失败"处理，也不该无限重连。
            yield _end_frame(
                run_id,
                cursor=cursor,
                status=last_status or AgentRunStatus.QUEUED,
                reason=RunStreamEndReason.RUN_DISAPPEARED,
            ).to_server_sent_event()
            return
        if run.status is not last_status:
            last_status = run.status
            yield RunStreamFrame(
                contract_id=RUN_STREAM_CONTRACT_ID,
                kind=RunStreamFrameKind.RUN_SNAPSHOT,
                run_id=run_id,
                cursor=cursor,
                status=run.status,
            ).to_server_sent_event()
        events = await source.get_events_after(run_id, after_sequence=cursor)
        for event in events or ():
            cursor = event.sequence
            yield RunStreamFrame(
                contract_id=RUN_STREAM_CONTRACT_ID,
                kind=RunStreamFrameKind.RUN_EVENT,
                run_id=run_id,
                cursor=cursor,
                status=run.status,
                event=event,
            ).to_server_sent_event()
        if run.status in TERMINAL_RUN_STATUSES:
            yield _end_frame(
                run_id,
                cursor=cursor,
                status=run.status,
                reason=RunStreamEndReason(run.status.value),
            ).to_server_sent_event()
            return
        if monotonic() >= deadline:
            # 超时结束是连接级事件而不是 run 级事件：客户端应带最后游标重连，或退回轮询。
            yield _end_frame(
                run_id,
                cursor=cursor,
                status=run.status,
                reason=RunStreamEndReason.STREAM_TIMEOUT,
            ).to_server_sent_event()
            return
        await asyncio.sleep(config.poll_seconds)


def _end_frame(
    run_id: str,
    *,
    cursor: int,
    status: AgentRunStatus,
    reason: RunStreamEndReason,
) -> RunStreamFrame:
    """构造结束帧，确保任何退出路径都显式告知客户端游标、状态和原因。

    没有结束帧的 SSE 断开在浏览器里表现为自动重连，演示时会看起来像服务端在抖动；显式结束帧让
    客户端能区分"该停了"和"该续传了"。
    """

    return RunStreamFrame(
        contract_id=RUN_STREAM_CONTRACT_ID,
        kind=RunStreamFrameKind.STREAM_END,
        run_id=run_id,
        cursor=cursor,
        status=status,
        end_reason=reason,
    )


def resolve_stream_cursor(
    *,
    last_event_id: str | None,
    after_sequence: int,
) -> int:
    """把浏览器 `Last-Event-ID` 与显式查询参数归一化为一个非负游标。

    重连头优先于查询参数，因为它反映客户端真正收到的最后一帧；无法解析或为负时静默退回参数值，
    这样一个损坏的重连头只会导致重复推送已知事件，而不是让整条请求 400 失败。
    """

    if last_event_id is not None:
        try:
            parsed = int(last_event_id)
        except ValueError:
            return after_sequence
        if parsed >= 0:
            return parsed
    return after_sequence
