"""验证 run-stream:v1 的帧不变量、增量续传语义与结束原因分类。

测试使用确定性替身 source，不启动数据库、Worker 或真实 SSE 连接；这样断言的是推流协议本身
（游标从哪来、状态何时推、结束帧凭什么原因结束），而不是 uvicorn 的传输行为。所有构造的事件
payload 只含计数与枚举，不含 Thought、Prompt 或凭据，与生产投影的安全面保持一致。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.api.streaming import (
    RUN_STREAM_CONTRACT_ID,
    RunStreamConfig,
    RunStreamEndReason,
    RunStreamFrame,
    RunStreamFrameKind,
    iter_run_stream,
    resolve_stream_cursor,
)
from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.orchestration.run_models import (
    AgentRunSnapshot,
    AgentRunStatus,
    RunEventPhase,
    RunPublicEvent,
)

RUN_ID = "run_00000000000000ab"
SESSION_ID = "session_00000000000000ab"
NOW = datetime(2026, 7, 16, 3, 0, tzinfo=UTC)


def _snapshot(status: AgentRunStatus) -> AgentRunSnapshot:
    """构造指定状态的最小合法 run 快照，供推流断言复用。

    终态需要 started_at/completed_at 与正尝试数，queued 则必须三者皆空；把这些组合集中在一个
    工厂里，避免每个测试各自拼一份而在 AgentRunSnapshot 校验规则变化时散落失败。
    """

    started = None if status is AgentRunStatus.QUEUED else NOW
    terminal = status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
    unhappy = terminal and status is not AgentRunStatus.COMPLETED
    return AgentRunSnapshot(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        status=status,
        user_query="LTS 合成任务失败",
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
        history_trigger=HistoryTrigger.NOT_REQUESTED,
        error_code="diagnosis_failed" if unhappy else None,
        error_message="诊断未完成。" if unhappy else None,
        created_at=NOW,
        started_at=started,
        completed_at=NOW if terminal else None,
        updated_at=NOW,
        attempt_count=0 if status is AgentRunStatus.QUEUED else 1,
    )


def _event(sequence: int) -> RunPublicEvent:
    """构造一条只含计数的安全公开事件，序号由调用方指定。

    event_id 使用确定性十六进制填充而不是随机值，因此断言可以直接比对序号而不必先读回对象；
    payload 刻意只放一个整数，证明推流不需要任何敏感字段就能渲染时间线。
    """

    return RunPublicEvent(
        event_id=f"run_evt_{sequence:016x}",
        run_id=RUN_ID,
        sequence=sequence,
        phase=RunEventPhase.REACT,
        event_type="observation_recorded",
        summary=f"第 {sequence} 条公开事件。",
        payload={"evidence_count": sequence},
        created_at=NOW,
    )


class ScriptedStreamSource:
    """按脚本逐轮返回 run 快照与增量事件的确定性推流数据源。

    每次 `get_run` 消费脚本的下一格，`get_events_after` 则返回该格中序号大于游标的事件，因此可以
    精确编排"状态先变、事件后到"这类真实竞态，而不依赖 sleep 或真实数据库提交顺序。
    """

    def __init__(
        self,
        ticks: list[tuple[AgentRunSnapshot | None, tuple[RunPublicEvent, ...]]],
    ) -> None:
        """记录脚本与调用轨迹，初始化时不产生任何 I/O。

        保存 `cursors` 是为了断言生成器每轮都带着最新游标去查询，而不是反复重读整条时间线——
        后者在长 run 上会让每次轮询的成本随事件数线性增长。
        """

        self._ticks = ticks
        self._index = -1
        self.cursors: list[int] = []

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """推进到脚本的下一格并返回该格的 run 快照，脚本耗尽时抛错。

        生成器每轮必须先读快照，因此这里同时充当轮次计数器；脚本耗尽说明生成器没有按预期结束，
        显式失败比返回默认值更容易定位。
        """

        self._index += 1
        if self._index >= len(self._ticks):
            raise AssertionError("run stream polled more ticks than the script provides")
        return self._ticks[self._index][0]

    async def get_events_after(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> tuple[RunPublicEvent, ...] | None:
        """返回当前格中序号大于游标的事件，并记录本轮使用的游标。

        过滤逻辑与 SQL 侧 `sequence > after_sequence` 一致，因此脚本可以重复给出同一批事件而不会
        造成重复推送，这正是仓储实现所保证的性质。
        """

        self.cursors.append(after_sequence)
        events = self._ticks[self._index][1]
        return tuple(event for event in events if event.sequence > after_sequence)


async def _collect(source: ScriptedStreamSource, **kwargs: object) -> list[dict[str, object]]:
    """把生成器产出的 SSE 事件收敛为帧 kind/id 的轻量列表，便于顺序断言。

    只提取 `event` 名与 `id`，因为帧正文的字段语义已由 `RunStreamFrame` 校验；断言顺序而不是
    逐字节比对 SSE 文本，可以避免测试锁死 sse-starlette 的编码细节。
    """

    frames: list[dict[str, object]] = []
    async for item in iter_run_stream(source, RUN_ID, config=RunStreamConfig(**kwargs)):  # type: ignore[arg-type]
        frames.append({"kind": item.event, "id": item.id})
    return frames


@pytest.mark.asyncio
async def test_stream_pushes_status_once_then_events_and_completes() -> None:
    """验证状态只在变化时推送、事件逐条推送，并以终态原因结束。

    第二轮 run 状态未变，因此不应重复推快照；结束帧的 `id` 必须等于最后一条事件序号，客户端凭它
    重连才不会漏事件或重复渲染。终态用 failed 而不是 completed，是因为 completed 快照必须携带完整
    `DiagnosisRunResult`，而推流的行为与结果正文无关，用最小合法终态断言更能隔离被测逻辑。
    """

    source = ScriptedStreamSource(
        [
            (_snapshot(AgentRunStatus.RUNNING), (_event(1),)),
            (_snapshot(AgentRunStatus.RUNNING), (_event(1), _event(2))),
            (_snapshot(AgentRunStatus.FAILED), (_event(1), _event(2), _event(3))),
        ]
    )

    frames = await _collect(source, poll_seconds=0.001, keepalive_seconds=0.5, max_seconds=5)

    assert frames == [
        {"kind": RunStreamFrameKind.RUN_SNAPSHOT.value, "id": "0"},
        {"kind": RunStreamFrameKind.RUN_EVENT.value, "id": "1"},
        {"kind": RunStreamFrameKind.RUN_EVENT.value, "id": "2"},
        {"kind": RunStreamFrameKind.RUN_SNAPSHOT.value, "id": "2"},
        {"kind": RunStreamFrameKind.RUN_EVENT.value, "id": "3"},
        {"kind": RunStreamFrameKind.STREAM_END.value, "id": "3"},
    ]
    assert source.cursors == [0, 1, 2]


@pytest.mark.asyncio
async def test_stream_reads_run_before_events_so_terminal_tick_keeps_final_events() -> None:
    """验证终态那一轮仍会先推完该事务提交的最后一批事件才发结束帧。

    `complete_run` 把终态状态与最后几条事件写在同一事务里，若生成器先判终态再读事件，时间线就会
    在"报告已生成"这类关键事件上被截断；本用例把三条事件全部压在终态轮，确保顺序不被回退，并顺带
    断言 cancelled 状态映射为同名结束原因。
    """

    source = ScriptedStreamSource(
        [(_snapshot(AgentRunStatus.CANCELLED), (_event(1), _event(2), _event(3)))]
    )

    frames: list[RunStreamFrame] = []
    async for item in iter_run_stream(
        source,
        RUN_ID,
        config=RunStreamConfig(poll_seconds=0.001, keepalive_seconds=0.5, max_seconds=5),
    ):
        frames.append(RunStreamFrame.model_validate_json(item.data))

    assert [frame.kind for frame in frames] == [
        RunStreamFrameKind.RUN_SNAPSHOT,
        RunStreamFrameKind.RUN_EVENT,
        RunStreamFrameKind.RUN_EVENT,
        RunStreamFrameKind.RUN_EVENT,
        RunStreamFrameKind.STREAM_END,
    ]
    assert frames[-1].cursor == 3
    assert frames[-1].end_reason is RunStreamEndReason.CANCELLED


@pytest.mark.asyncio
async def test_stream_resumes_after_cursor_without_replaying_known_events() -> None:
    """验证带游标续传只推送新事件，重连不会让前端重复渲染整条时间线。

    浏览器重连会带上 `Last-Event-ID`，服务端据此把游标定位到 2；断言输出只含序号 3 的事件，
    证明去重发生在查询层而不是依赖客户端自行过滤。
    """

    source = ScriptedStreamSource(
        [(_snapshot(AgentRunStatus.FAILED), (_event(1), _event(2), _event(3)))]
    )

    frames: list[dict[str, object]] = []
    async for item in iter_run_stream(
        source,
        RUN_ID,
        after_sequence=2,
        config=RunStreamConfig(poll_seconds=0.001, keepalive_seconds=0.5, max_seconds=5),
    ):
        frames.append({"kind": item.event, "id": item.id})

    assert frames == [
        {"kind": RunStreamFrameKind.RUN_SNAPSHOT.value, "id": "2"},
        {"kind": RunStreamFrameKind.RUN_EVENT.value, "id": "3"},
        {"kind": RunStreamFrameKind.STREAM_END.value, "id": "3"},
    ]
    assert source.cursors == [2]


@pytest.mark.asyncio
async def test_missing_run_ends_stream_with_run_disappeared_instead_of_failure() -> None:
    """验证推流期间 run 消失时以 run_disappeared 结束，而不是伪装成诊断失败。

    把外部清理报成 failed 会让演示者以为系统出错；分类为连接侧原因后客户端可以停止重连并提示
    "该 run 已不存在"，与真正的 failed run 明确区分。
    """

    source = ScriptedStreamSource([(None, ())])

    frames: list[RunStreamFrame] = []
    async for item in iter_run_stream(
        source,
        RUN_ID,
        config=RunStreamConfig(poll_seconds=0.001, keepalive_seconds=0.5, max_seconds=5),
    ):
        frames.append(RunStreamFrame.model_validate_json(item.data))

    assert len(frames) == 1
    assert frames[0].end_reason is RunStreamEndReason.RUN_DISAPPEARED
    assert frames[0].status is AgentRunStatus.QUEUED


class StaticStreamSource:
    """始终返回同一份 running 快照与同一批事件的推流数据源。

    连接寿命耗尽的场景需要 run 长期停在非终态，因此这里不消费脚本、可被无限轮询；`polls` 计数
    则用于确认生成器确实是被寿命预算而不是被数据耗尽逼停的。
    """

    def __init__(self, run: AgentRunSnapshot, events: tuple[RunPublicEvent, ...]) -> None:
        """保存固定快照与事件批次，并初始化轮询计数。

        构造不做任何 I/O，也不复制事件：帧模型是 frozen 的，推流不会修改传入对象，因此共享同一份
        元组既安全又能让断言直接比对身份。
        """

        self._run = run
        self._events = events
        self.polls = 0

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """返回固定的 running 快照并累加轮询计数。

        状态永不变化，因此生成器只会在第一轮推送一次快照帧，后续轮次的输出完全取决于事件与预算，
        使超时断言不被重复快照干扰。
        """

        self.polls += 1
        return self._run

    async def get_events_after(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> tuple[RunPublicEvent, ...] | None:
        """返回序号大于游标的固定事件，游标推进后自然变为空批次。

        这正是真实仓储在"没有新事件"时的行为：返回空元组而不是 None，因为 None 专门表示 run 不存在，
        两者混用会让推流把"暂时没进展"误判成"run 消失了"。
        """

        return tuple(event for event in self._events if event.sequence > after_sequence)


@pytest.mark.asyncio
async def test_exhausted_lifetime_ends_with_stream_timeout_on_a_live_run() -> None:
    """验证连接预算耗尽时结束原因是 stream_timeout 且 run 状态仍为 running。

    连接寿命是读侧预算，与 run 自身无关；结束帧必须同时给出 running 状态和最后游标，客户端才能
    判断"应该带游标重连"而不是"诊断已经结束"。数据源可被无限轮询，因此只有寿命预算能终止这条流。
    """

    source = StaticStreamSource(_snapshot(AgentRunStatus.RUNNING), (_event(1),))

    frames: list[RunStreamFrame] = []
    async for item in iter_run_stream(
        source,
        RUN_ID,
        config=RunStreamConfig(poll_seconds=0.001, keepalive_seconds=0.5, max_seconds=1.01),
    ):
        frames.append(RunStreamFrame.model_validate_json(item.data))

    assert frames[-1].end_reason is RunStreamEndReason.STREAM_TIMEOUT
    assert frames[-1].status is AgentRunStatus.RUNNING
    assert frames[-1].cursor == 1
    assert source.polls > 1


def test_event_frame_requires_matching_run_and_cursor() -> None:
    """验证事件帧不能混 run、也不能让游标与事件序号脱节。

    游标是客户端唯一的续传依据，一旦帧里的 `id` 与事件序号不一致，重连就会漏掉或重复事件；因此
    这类组合必须在推送前失败，而不是靠前端事后校对。
    """

    with pytest.raises(ValidationError):
        RunStreamFrame(
            contract_id=RUN_STREAM_CONTRACT_ID,
            kind=RunStreamFrameKind.RUN_EVENT,
            run_id=RUN_ID,
            cursor=2,
            status=AgentRunStatus.RUNNING,
            event=_event(1),
        )
    with pytest.raises(ValidationError):
        RunStreamFrame(
            contract_id=RUN_STREAM_CONTRACT_ID,
            kind=RunStreamFrameKind.RUN_EVENT,
            run_id=RUN_ID,
            cursor=1,
            status=AgentRunStatus.RUNNING,
        )


def test_snapshot_and_end_frames_reject_mismatched_optional_payloads() -> None:
    """验证快照帧不得携带事件或结束原因，结束帧必须携带原因。

    封闭的帧类型加上互斥的可选字段让客户端可以按 `event` 名直接分发，无需对每个字段判空；
    否则前端迟早会渲染出一条空事件或一个没有原因的结束提示。
    """

    with pytest.raises(ValidationError):
        RunStreamFrame(
            contract_id=RUN_STREAM_CONTRACT_ID,
            kind=RunStreamFrameKind.RUN_SNAPSHOT,
            run_id=RUN_ID,
            cursor=1,
            status=AgentRunStatus.RUNNING,
            event=_event(1),
        )
    with pytest.raises(ValidationError):
        RunStreamFrame(
            contract_id=RUN_STREAM_CONTRACT_ID,
            kind=RunStreamFrameKind.STREAM_END,
            run_id=RUN_ID,
            cursor=1,
            status=AgentRunStatus.COMPLETED,
        )


def test_stream_config_requires_poll_under_keepalive_under_lifetime() -> None:
    """验证轮询/心跳/寿命的有序约束在配置阶段就被拒绝。

    轮询慢于心跳时"实时"名不副实，心跳不短于寿命时连接根本等不到第一次心跳；把这两个矛盾放在
    配置校验里，可以避免部署后只能靠观察浏览器行为来发现配置无效。
    """

    with pytest.raises(ValidationError):
        RunStreamConfig(poll_seconds=2, keepalive_seconds=1, max_seconds=10)
    with pytest.raises(ValidationError):
        RunStreamConfig(poll_seconds=0.5, keepalive_seconds=10, max_seconds=10)


def test_last_event_id_wins_over_query_and_bad_values_fall_back() -> None:
    """验证重连头优先于查询参数，非法或负值静默退回参数值。

    重连头反映客户端真正收到的最后一帧，因此优先级更高；而一个被代理改坏的头只应导致重复推送
    已知事件，不能让整条推流请求 400 失败——那会把可恢复的抖动升级成演示中断。
    """

    assert resolve_stream_cursor(last_event_id="7", after_sequence=2) == 7
    assert resolve_stream_cursor(last_event_id="0", after_sequence=5) == 0
    assert resolve_stream_cursor(last_event_id="abc", after_sequence=5) == 5
    assert resolve_stream_cursor(last_event_id="-3", after_sequence=5) == 5
    assert resolve_stream_cursor(last_event_id=None, after_sequence=4) == 4
