"""PostgreSQL 调用链集成测试：span 与 run 终态同事务落库、约束防护、读回与指标聚合。

单元测试只能验证采集器在内存里的形状，而这一层真正会出错的地方全在数据库：timestamptz 时区、
JSONB 属性往返、表级 CheckConstraint 是否真的拦住绕过应用层的手工写入，以及 `/metrics` 依赖的
GROUP BY 是否算出与 span 明细一致的错误数。这些缺陷的共同特征是"接口照常返回 200，只是时间轴
或错误率是错的"，因此必须用真实 SQL 覆盖。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.observability import (
    RunTrace,
    RunTraceCollector,
    TraceSpanKind,
    TraceSpanStatus,
    bind_run_trace_collector,
    make_span_id,
    reset_run_trace_collector,
    trace_span,
)
from app.orchestration.run_models import DiagnosisMessage, RunEventPhase, RunPublicEvent
from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.run_repository import PostgresDiagnosisRunRepository

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
SESSION_ID = "session_00000000000000ab"
RUN_ID = "run_00000000000000ab"
WORKER_ID = "worker_00000000000000ab"


def _message() -> DiagnosisMessage:
    """构造一条通过能力路由校验的 LTS 单组件合成消息，用于建立可 claim 的 run。

    trace 测试不关心诊断内容，但 run 行必须真实通过领域校验才能进入 queued 状态；使用合成文本
    而不是最小占位符，可以顺带证明中文用户问题不会影响 span 落库路径。
    """

    return DiagnosisMessage(
        content="检查 LTS 合成任务的调用链",
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
        history_trigger=HistoryTrigger.NOT_REQUESTED,
    )


def _build_trace(run_id: str) -> RunTrace:
    """用真实采集器生成一棵"工作流 → 节点 → 失败工具调用 + 模型调用"的四层 trace。

    刻意通过 `trace_span` 而不是手工构造 ``TraceSpan``：这样落库的数据与生产路径完全一致，
    包括父指针推导与单调时钟耗时，避免测试用一份"理想化"的 trace 掩盖插桩本身的缺陷。
    """

    collector = RunTraceCollector(run_id)
    token = bind_run_trace_collector(collector)
    try:
        with trace_span(TraceSpanKind.WORKFLOW, "diagnosis.workflow"):
            with trace_span(TraceSpanKind.NODE, "diagnosis.run_react"):
                with trace_span(
                    TraceSpanKind.TOOL_CALL,
                    "react.tool_call",
                    tool_name="lts_job_status",
                ) as span:
                    span.annotate(ok=False, attempt_count=2)
                    span.mark(TraceSpanStatus.ERROR)
                with trace_span(TraceSpanKind.MODEL_CALL, "model.chat_completion", role="planner"):
                    pass
    finally:
        reset_run_trace_collector(token)
    return collector.snapshot()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_persists_reads_and_aggregates_run_trace_spans() -> None:
    """验证 span 与 failed 终态同事务落库、读回一致、约束生效并被 /metrics 聚合。

    测试用 failed run 而不是 completed，是因为失败 run 的 trace 价值最高——它是唯一能说明"失败前
    走到了哪一层"的结构化证据，也顺带证明 span 不依赖成功结果或 checkpoint。随后逐条尝试用原始
    SQL 写入负耗时、未知层级、自引用父指针和重复序号，确认表级约束会拒绝绕过 ORM 的写入；最后
    删除 run 验证级联清理，避免 span 表无界增长。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")

    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    now = datetime.now(UTC) - timedelta(hours=1)
    trace = _build_trace(RUN_ID)
    assert [span.kind for span in trace.spans] == [
        TraceSpanKind.WORKFLOW,
        TraceSpanKind.NODE,
        TraceSpanKind.TOOL_CALL,
        TraceSpanKind.MODEL_CALL,
    ]

    try:
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM session_checkpoints"))
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM agent_runs"))
            await session.execute(text("DELETE FROM diagnosis_sessions"))

        async with factory.begin() as session:
            repository = PostgresDiagnosisRunRepository(session)
            await repository.create_session(session_id=SESSION_ID, title="trace synthetic", now=now)
            await repository.create_run(
                run_id=RUN_ID,
                session_id=SESSION_ID,
                message=_message(),
                now=now,
            )
            claimed = await repository.claim_next_run(
                worker_id=WORKER_ID,
                now=now,
                lease_seconds=120,
                max_attempts=3,
            )
            assert claimed is not None and claimed.run.run_id == RUN_ID
            # trace 与 failed 终态共用这一个事务：要么两者都可见，要么都不可见。
            await repository.fail_run(
                RUN_ID,
                error_code="diagnosis_execution_failed",
                error_message="合成失败摘要，不含内部异常文本。",
                event=RunPublicEvent(
                    event_id="run_evt_00000000000000ab",
                    run_id=RUN_ID,
                    sequence=1,
                    phase=RunEventPhase.SYSTEM,
                    event_type="run_failed",
                    summary="合成 run 已失败。",
                    created_at=now,
                ),
                worker_id=WORKER_ID,
                now=now,
                trace=trace,
            )

        async with factory() as session:
            repository = PostgresDiagnosisRunRepository(session)
            stored = await repository.list_trace_spans(RUN_ID)
            assert stored is not None
            assert stored.spans == trace.spans
            assert stored.dropped_span_count == 0
            assert stored.total_duration_ms == trace.spans[0].duration_ms
            tool_span = stored.spans_by_kind(TraceSpanKind.TOOL_CALL)[0]
            assert tool_span.status is TraceSpanStatus.ERROR
            assert tool_span.attributes == {
                "tool_name": "lts_job_status",
                "ok": False,
                "attempt_count": 2,
            }
            # 时间戳必须带时区读回：naive 值会让跨容器时间轴静默偏移而断言仍能通过。
            assert all(span.started_at.tzinfo is not None for span in stored.spans)

            # 未知 run 与"run 存在但无 span"必须可区分，否则调用方无法分辨用错 ID 还是未开采集。
            assert await repository.list_trace_spans("run_ffffffffffffffff") is None

        async with factory() as session:
            repository = PostgresDiagnosisRunRepository(session)
            snapshot = await repository.aggregate_metrics()
            assert snapshot.run_counts == {"failed": 1}
            by_name = {metric.name: metric for metric in snapshot.spans}
            assert set(by_name) == {
                "diagnosis.workflow",
                "diagnosis.run_react",
                "react.tool_call",
                "model.chat_completion",
            }
            assert by_name["react.tool_call"].error_count == 1
            assert by_name["diagnosis.workflow"].error_count == 0
            assert by_name["react.tool_call"].kind == "tool_call"
            assert by_name["diagnosis.workflow"].duration_ms_max >= 0

        for statement in (
            "UPDATE run_trace_spans SET duration_ms = -1 WHERE sequence = 1",
            "UPDATE run_trace_spans SET kind = 'guesswork' WHERE sequence = 1",
            "UPDATE run_trace_spans SET status = 'degraded' WHERE sequence = 1",
            "UPDATE run_trace_spans SET parent_span_id = span_id WHERE sequence = 2",
            "UPDATE run_trace_spans SET ended_at = started_at - interval '1 second' "
            "WHERE sequence = 1",
            "UPDATE run_trace_spans SET sequence = 1 WHERE sequence = 2",
        ):
            async with factory() as session:
                # 逐条独立事务：约束一旦触发整个事务即中止，合并执行会让后续语句无法被验证。
                with pytest.raises(IntegrityError):
                    await session.execute(text(statement))
                    await session.flush()
                await session.rollback()

        async with factory() as session:
            repository = PostgresDiagnosisRunRepository(session)
            with pytest.raises(ValueError, match="trace must belong to the persisted run"):
                repository._add_trace_spans("run_ffffffffffffffff", trace)

        async with factory.begin() as session:
            await session.execute(
                text("DELETE FROM agent_runs WHERE run_id = :run_id"),
                {"run_id": RUN_ID},
            )
        async with factory() as session:
            # ON DELETE CASCADE：没有 run 的 span 树无法解释，留着只会让表无界增长。
            remaining = await session.scalar(
                text("SELECT count(*) FROM run_trace_spans WHERE run_id = :run_id"),
                {"run_id": RUN_ID},
            )
            assert remaining == 0
            assert make_span_id(RUN_ID, 1) == trace.spans[0].span_id
    finally:
        # 断言失败也要反序清理并释放 asyncpg 池，避免污染其他 postgres marker 用例。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM session_checkpoints"))
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM agent_runs"))
            await session.execute(text("DELETE FROM diagnosis_sessions"))
        await engine.dispose()
