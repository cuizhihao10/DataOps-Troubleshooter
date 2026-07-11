"""验证 diagnosis session/run/event 迁移、应用 runtime 成功/失败持久化和轮询读取。

测试使用真实 PostgreSQL/SQLAlchemy 与资源仓储，GraphRAG/顶层 workflow 使用强类型替身，不访问
模型或 MCP。它覆盖同步首版从 message 到 completed/failed run 的事务边界和安全事件投影。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.capabilities import (
    DiagnosisIntent,
    HistoryTrigger,
    get_capability_registry,
)
from app.domain.models import (
    AuditResult,
    AuditStatus,
    DiagnosisReport,
)
from app.memory.models import MemoryStageResult, MemoryStageStatus
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    DIAGNOSIS_WORKFLOW_CONTRACT_ID,
    REACT_LOOP_CONTRACT_ID,
    DiagnosisMessage,
    DiagnosisRunResult,
    ReactEventType,
    ReactPublicEvent,
    ReactRunResult,
    ReportEventType,
    ReportPublicEvent,
    ReportRunResult,
    ReportWorkflowOutcome,
)
from app.orchestration.diagnosis_runtime import (
    DiagnosisApplicationRuntime,
    DiagnosisExecutionFailed,
    PostgresGraphContextRetriever,
)
from app.orchestration.run_models import AgentRunStatus, RunEventPhase
from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.models import DiagnosisSessionRecord, SessionCheckpointRecord
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.retrieval.models import (
    EvidenceBundleBudget,
    GraphEvidenceBundle,
    HybridScoringWeights,
    RetrievalMode,
)

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


class IncrementingClock:
    """每次调用返回比前一次增加一分钟的 UTC 时间，稳定验证资源时间单调性。

    时钟不读取系统时间或 sleep；调用次数超出也可继续生成，适合两次 success 与一次 failure 共享。
    """

    def __init__(self, start: datetime) -> None:
        """保存带时区起点并把调用计数初始化为零。

        naive 起点立即失败，避免 PostgreSQL timestamptz 断言依赖本地时区；实例只在当前测试使用。
        """

        if start.tzinfo is None:
            raise ValueError("incrementing clock requires a timezone")
        self._start = start
        self._calls = 0

    def __call__(self) -> datetime:
        """返回当前序列时间并推进一分钟，不产生外部副作用。

        第一次返回 start，后续严格递增；该行为让 created/started/completed/updated 的顺序可
        精确验证。
        """

        value = self._start + timedelta(minutes=self._calls)
        self._calls += 1
        return value


class PrefixIdSequence:
    """按 prefix 返回预设 session/run ID，额外调用或前缀错误时显式失败。

    测试可据此准确知道失败 run_id 并查询数据库；不使用随机 UUID，断言仍经过生产 pattern 校验。
    """

    def __init__(self) -> None:
        """初始化一个 session 和三个 run 的固定十六进制 ID 队列。

        队列按资源种类分开，调用顺序变化会通过空列表 AssertionError 暴露，不会重复最后 ID。
        """

        self._values = {
            "session": ["session_1111111111111111"],
            "run": [
                "run_2222222222222222",
                "run_3333333333333333",
                "run_4444444444444444",
            ],
        }

    def __call__(self, prefix: str) -> str:
        """弹出指定资源类型的下一个 ID，未知类型或耗尽时抛 AssertionError。

        方法不回收 ID，防止测试重放意外覆盖已完成 run；返回值格式与默认 UUID 工厂一致。
        """

        if prefix not in self._values or not self._values[prefix]:
            raise AssertionError(f"unexpected diagnosis ID request: {prefix}")
        return self._values[prefix].pop(0)


class EmptyGraphRetriever:
    """返回合法但无节点/路径的 GraphEvidenceBundle，并记录查询文本。

    空召回是正常检索结果而非依赖失败；应用 runtime 仍必须写 retrieval 事件并继续 workflow。
    """

    def __init__(self) -> None:
        """初始化空查询记录，不预先绑定固定用户问题。

        每次 retrieve 根据实际 query 创建 Bundle，保证 persisted run 与检索上下文一致。
        """

        self.queries: list[str] = []

    async def retrieve(self, query: str) -> GraphEvidenceBundle:
        """记录非空查询并返回 hybrid_graph 空证据 Bundle。

        used_bytes 设为零表示无 selected 主体；方法不抛异常或伪造知识节点，便于隔离资源持久化行为。
        """

        self.queries.append(query)
        return GraphEvidenceBundle(
            query=query,
            retrieval_mode=RetrievalMode.HYBRID_GRAPH,
            budget=EvidenceBundleBudget(),
            used_bytes=0,
        )


class SuccessfulTwiceThenFailingWorkflow:
    """前两次返回合法结果，第三次抛含敏感样本文本的内部异常。

    两次成功用于证明 checkpoint v1→v2 恢复；失败文本用于证明错误不会覆盖 v2 或进入 API。
    contexts 保存请求以断言第二轮获得上一轮公开会话上下文。
    """

    def __init__(self) -> None:
        """初始化空请求列表和零调用计数，不创建模型或数据库资源。

        第一次/第二次成功，第三次及以后失败，不提供隐藏默认成功路径。
        实例由单个测试独占，避免调用计数跨事件循环或测试用例共享。
        """

        self.requests = []
        self._calls = 0

    async def run(self, request) -> DiagnosisRunResult:
        """根据调用次数返回强类型 accepted 结果或抛内部 RuntimeError。

        成功路径使用真实 capability registry 和公开事件 Schema；第二次错误包含不得持久化的测试
        文本，应用 runtime 应以通用 diagnosis_execution_failed 替换。
        """

        self.requests.append(request)
        self._calls += 1
        if self._calls > 2:
            raise RuntimeError("internal synthetic secret must not persist")
        selection = get_capability_registry().select(request.capability_request)
        react_state = request.state.model_copy(
            update={
                "intent": selection.intent.value,
                "active_capabilities": [item.value for item in selection.active_capabilities],
                "stop_reason": "evidence_insufficient",
            }
        )
        react = ReactRunResult(
            contract_id=REACT_LOOP_CONTRACT_ID,
            state=react_state,
            capabilities=selection,
            events=[
                ReactPublicEvent(
                    event_id="react_evt_1111111111111111",
                    sequence=1,
                    event_type=ReactEventType.CAPABILITIES_SELECTED,
                    summary="测试能力选择完成。",
                ),
                ReactPublicEvent(
                    event_id="react_evt_2222222222222222",
                    sequence=2,
                    event_type=ReactEventType.LOOP_STOPPED,
                    summary="测试证据不足停止。",
                    stop_reason="evidence_insufficient",
                ),
            ],
        )
        report_state = react_state.model_copy(
            update={
                "draft_report": DiagnosisReport(
                    summary="证据不足，保留安全不确定性。",
                    uncertainties=["尚无实时工具证据确认根因。"],
                ),
                "audit_result": AuditResult(status=AuditStatus.ACCEPT),
                "memory_candidate": None,
            }
        )
        report = ReportRunResult(
            contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
            state=report_state,
            outcome=ReportWorkflowOutcome.ACCEPTED,
            events=[
                ReportPublicEvent(
                    event_id="report_evt_1111111111111111",
                    sequence=1,
                    event_type=ReportEventType.DRAFT_CREATED,
                    summary="测试安全草稿完成。",
                    revision_number=0,
                ),
                ReportPublicEvent(
                    event_id="report_evt_2222222222222222",
                    sequence=2,
                    event_type=ReportEventType.AUDIT_COMPLETED,
                    summary="测试审计接受无根因报告。",
                    audit_status=AuditStatus.ACCEPT,
                    revision_number=0,
                ),
            ],
        )
        return DiagnosisRunResult(
            contract_id=DIAGNOSIS_WORKFLOW_CONTRACT_ID,
            history_trigger=request.capability_request.history_trigger,
            react=react,
            report=report,
            memory_stage=MemoryStageResult(status=MemoryStageStatus.SKIPPED_NO_ROOT_CAUSE),
        )


def _message(content: str = "检查 LTS 合成任务") -> DiagnosisMessage:
    """构造通过 capability 路由校验的 LTS 单组件合成消息。

    history 默认 not_requested，便于测试资源 runtime 而不依赖长期记忆数据；内容会同时进入 run 和
    EmptyGraphRetriever 查询记录。
    """

    from app.domain.models import Component

    return DiagnosisMessage(
        content=content,
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
        history_trigger=HistoryTrigger.NOT_REQUESTED,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_diagnosis_resources_persist_success_failure_and_events() -> None:
    """贯通资源表与 checkpoint 迁移、追问恢复、失败保护和连续事件轮询。

    前两次 run 依次保存 checkpoint v1/v2，第二轮恢复上一公开报告；第三次异常保存安全 failed
    事件且不覆盖 v2。最后绕过仓储写非法 completed 组合，证明数据库约束生效。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    retriever = EmptyGraphRetriever()
    workflow = SuccessfulTwiceThenFailingWorkflow()
    runtime = DiagnosisApplicationRuntime(
        factory,
        retriever=retriever,
        workflow=workflow,
        now_factory=IncrementingClock(NOW),
        id_factory=PrefixIdSequence(),
    )

    try:
        # 四张资源表由本测试独占；按外键反序清理，不触碰知识图或长期记忆表。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM session_checkpoints"))
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM agent_runs"))
            await session.execute(text("DELETE FROM diagnosis_sessions"))

        actual_retriever = PostgresGraphContextRetriever(
            factory,
            DeterministicHashEmbeddingProvider(dimensions=128),
            score_weights=HybridScoringWeights(),
            budget=EvidenceBundleBudget(),
            seed_limit=2,
            max_hops=1,
        )
        long_bundle = await actual_retriever.retrieve("长" * 3000)
        assert len(long_bundle.query) == 2000

        created = await runtime.create_session(title="  PostgreSQL 合成会话  ")
        assert created.title == "PostgreSQL 合成会话"
        completed = await runtime.submit_message(created.session_id, _message())
        assert completed is not None
        assert completed.status is AgentRunStatus.COMPLETED
        assert completed.result is not None
        assert completed.result.react.state.run_id == completed.run_id
        assert retriever.queries == ["当前问题: 检查 LTS 合成任务"]

        reread = await runtime.get_run(completed.run_id)
        assert reread == completed
        events = await runtime.get_events(completed.run_id)
        assert events is not None
        assert [event.phase for event in events.events] == [
            RunEventPhase.RETRIEVAL,
            RunEventPhase.REACT,
            RunEventPhase.REACT,
            RunEventPhase.REPORT,
            RunEventPhase.REPORT,
            RunEventPhase.MEMORY,
            RunEventPhase.SYSTEM,
        ]
        assert [event.sequence for event in events.events] == list(range(1, 8))
        assert events.events[-1].event_type == "session_checkpoint_saved"
        assert events.events[-1].payload["checkpoint_version"] == 1

        followup = await runtime.submit_message(
            created.session_id,
            _message("这个恢复操作风险高吗？"),
        )
        assert followup is not None and followup.status is AgentRunStatus.COMPLETED
        assert len(workflow.requests) == 2
        restored_state = workflow.requests[1].state
        assert restored_state.run_id == followup.run_id
        assert restored_state.react_step == 0
        assert restored_state.session_context is not None
        assert restored_state.session_context.source_run_id == completed.run_id
        assert "上一报告摘要" in retriever.queries[1]
        followup_events = await runtime.get_events(followup.run_id)
        assert followup_events is not None
        assert followup_events.events[0].payload["restored_checkpoint_version"] == 1
        assert followup_events.events[-1].payload["checkpoint_version"] == 2

        async with factory() as session:
            checkpoint_record = await session.get(SessionCheckpointRecord, created.session_id)
            assert checkpoint_record is not None
            assert checkpoint_record.checkpoint_version == 2
            assert checkpoint_record.source_run_id == followup.run_id
            assert checkpoint_record.snapshot["contract_id"] == "session-checkpoint:v1"

        async with factory() as session:
            # 绕过仓储写内部 phase，证明数据库不会把未审查事件类别混入公开时间线。
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO run_events "
                        "(event_id, run_id, sequence, phase, event_type, summary, payload) "
                        "VALUES ('run_evt_invalid_phase', :run_id, 99, 'internal', "
                        "'raw_model_output', 'invalid', '{}'::jsonb)"
                    ),
                    {"run_id": completed.run_id},
                )
                await session.flush()
            await session.rollback()

        with pytest.raises(DiagnosisExecutionFailed) as captured:
            await runtime.submit_message(created.session_id, _message())
        failed_id = captured.value.run_id
        failed = await runtime.get_run(failed_id)
        assert failed is not None and failed.status is AgentRunStatus.FAILED
        assert failed.error_code == "diagnosis_execution_failed"
        assert "internal synthetic secret" not in failed.error_message
        failed_events = await runtime.get_events(failed_id)
        assert failed_events is not None
        assert len(failed_events.events) == 1
        assert failed_events.events[0].phase is RunEventPhase.SYSTEM
        assert "internal synthetic secret" not in str(failed_events.model_dump())

        async with factory() as session:
            unchanged_checkpoint = await session.get(SessionCheckpointRecord, created.session_id)
            assert unchanged_checkpoint is not None
            assert unchanged_checkpoint.checkpoint_version == 2
            assert unchanged_checkpoint.source_run_id == followup.run_id

        async with factory() as session:
            stored_session = await session.scalar(
                select(DiagnosisSessionRecord).where(
                    DiagnosisSessionRecord.session_id == created.session_id
                )
            )
            assert stored_session is not None
            assert stored_session.last_user_query_summary == "检查 LTS 合成任务"

            # 直接制造 completed 但 result 为空的非法组合，数据库约束必须拒绝而非依赖仓储自律。
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "UPDATE agent_runs SET status='completed', error_code=NULL, "
                        "error_message=NULL WHERE run_id=:run_id"
                    ),
                    {"run_id": failed_id},
                )
                await session.flush()
            await session.rollback()
    finally:
        # 任一断言失败后仍反序清理资源并释放 asyncpg 池，避免污染其他 postgres marker。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM session_checkpoints"))
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(text("DELETE FROM agent_runs"))
            await session.execute(text("DELETE FROM diagnosis_sessions"))
        await engine.dispose()
