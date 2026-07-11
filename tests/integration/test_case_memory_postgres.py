"""验证案例记忆迁移、pgvector 去重、run 幂等、状态决策和 confirmed 召回。

测试使用本地 PostgreSQL/pgvector 和真实 asyncpg/SQLAlchemy 仓储，不访问模型 API；Embedding 使用
确定性 Provider。它覆盖从 accepted ReportRunResult 到 pending/confirmed/rejected 的最小闭环。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.domain.models import (
    AgentState,
    AuditResult,
    AuditStatus,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
)
from app.memory.models import MemoryDecision, MemoryDuplicateType, MemoryStageStatus
from app.memory.runtime import PostgresMemoryRuntime
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    ReportEventType,
    ReportPublicEvent,
    ReportRunResult,
    ReportWorkflowOutcome,
)
from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.models import MemoryEvidenceRecord
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


def _accepted_result(
    *,
    run_id: str,
    evidence_id: str,
    root_cause: str = "上游数据未按时就绪",
    observed_at: datetime = NOW,
) -> ReportRunResult:
    """构造可被长期记忆 Service 接受的合成报告终态。

    根因、假设和 Evidence 精确对齐；可覆盖 run/evidence/root 文本以分别验证精确签名与向量去重。
    事件只满足工作流终态，不携带模型原始推理。
    """

    evidence = Evidence(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.TOOL,
        source_id=f"source_{run_id}",
        content="合成工具确认上游数据未就绪。",
        observed_at=observed_at,
        reliability=0.95,
    )
    report = DiagnosisReport(
        summary="已确认合成根因。",
        root_causes=[
            RootCauseConclusion(
                root_cause=root_cause,
                confidence=0.9,
                evidence_refs=[evidence_id],
            )
        ],
        evidence_refs=[evidence_id],
        remediation_steps=[
            RemediationStep(
                order=1,
                action="在隔离环境补齐上游数据后人工复核。",
                risk_level=RiskLevel.MEDIUM,
                evidence_refs=[evidence_id],
                prerequisites=["确认数据快照和审批。"],
                rollback="恢复补数前快照。",
                verification="重新执行只读状态检查。",
            )
        ],
        risks=["需要人工审批。"],
    )
    state = AgentState(
        run_id=run_id,
        session_id=f"session_{run_id}",
        user_query="检查合成任务",
        intent="single_component_diagnosis",
        active_capabilities=[
            "single_component_diagnosis",
            "risk_assessment",
            "structured_reporting",
        ],
        hypotheses=[
            FaultHypothesis(
                hypothesis_id=f"hyp_{run_id}",
                symptom="LTS 任务等待上游",
                candidate_root_cause=root_cause,
                components=[Component.LTS],
                supporting_evidence=[evidence_id],
                status=HypothesisStatus.CONFIRMED,
                confidence=0.9,
            )
        ],
        evidence=[evidence],
        stop_reason="evidence_sufficient",
        draft_report=report,
        audit_result=AuditResult(status=AuditStatus.ACCEPT),
    )
    return ReportRunResult(
        contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
        state=state,
        outcome=ReportWorkflowOutcome.ACCEPTED,
        events=[
            ReportPublicEvent(
                event_id="report_evt_1234567890abcdef",
                sequence=1,
                event_type=ReportEventType.DRAFT_CREATED,
                summary="合成草稿完成。",
                revision_number=0,
            ),
            ReportPublicEvent(
                event_id="report_evt_abcdef1234567890",
                sequence=2,
                event_type=ReportEventType.AUDIT_COMPLETED,
                summary="合成审计接受。",
                audit_status=AuditStatus.ACCEPT,
                revision_number=0,
            ),
        ],
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_case_memory_staging_dedup_decision_and_search() -> None:
    """贯通真实迁移表、advisory lock、签名/vector 去重、证据关联和 confirmed 搜索。

    同 run 重放不增加 occurrence；第二 run 精确合并；第三种根因在阈值零时向量合并并保留 canonical
    根因。pending/rejected 搜索为空，confirm/reconfirm 可见，数据库状态约束拒绝非法值。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    runtime = PostgresMemoryRuntime(
        factory,
        DeterministicHashEmbeddingProvider(dimensions=128),
        dedup_similarity_threshold=0.0,
        default_search_limit=5,
    )
    try:
        # 测试独占案例表内容，知识图种子保持不变；先删关联再删主表满足外键顺序。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))

        first = await runtime.stage(
            _accepted_result(
                run_id="run_memory_pg_001",
                evidence_id="ev_memory_pg_001",
            )
        )
        assert first.status is MemoryStageStatus.STAGED
        assert first.memory is not None
        memory_id = first.memory.memory_id
        assert first.memory.status is MemoryStatus.PENDING
        assert await runtime.search("上游未就绪") == []

        replay = await runtime.stage(
            _accepted_result(
                run_id="run_memory_pg_001",
                evidence_id="ev_memory_pg_001",
            )
        )
        second = await runtime.stage(
            _accepted_result(
                run_id="run_memory_pg_002",
                evidence_id="ev_memory_pg_002",
                observed_at=NOW + timedelta(minutes=1),
            )
        )
        vector_duplicate = await runtime.stage(
            _accepted_result(
                run_id="run_memory_pg_003",
                evidence_id="ev_memory_pg_003",
                root_cause="上游数据到达延迟",
                observed_at=NOW + timedelta(minutes=2),
            )
        )

        assert replay.status is MemoryStageStatus.MERGED
        assert replay.memory is not None and replay.memory.occurrence_count == 1
        assert second.duplicate_type is MemoryDuplicateType.EXACT_SIGNATURE
        assert second.memory is not None and second.memory.occurrence_count == 2
        assert vector_duplicate.duplicate_type is MemoryDuplicateType.VECTOR_SIMILARITY
        assert vector_duplicate.memory is not None
        assert vector_duplicate.memory.memory_id == memory_id
        assert vector_duplicate.memory.root_cause == "上游数据未按时就绪"
        assert vector_duplicate.memory.occurrence_count == 3
        assert vector_duplicate.memory.evidence_refs == [
            "ev_memory_pg_001",
            "ev_memory_pg_002",
            "ev_memory_pg_003",
        ]

        confirmed = await runtime.decide(memory_id, MemoryDecision.CONFIRM)
        assert confirmed is not None and confirmed.status is MemoryStatus.CONFIRMED
        matches = await runtime.search("LTS 上游数据未就绪")
        assert matches and matches[0].memory.memory_id == memory_id
        assert matches[0].memory.status is MemoryStatus.CONFIRMED
        assert 0 <= matches[0].similarity <= 1

        rejected = await runtime.decide(memory_id, MemoryDecision.REJECT)
        assert rejected is not None and rejected.status is MemoryStatus.REJECTED
        assert await runtime.search("LTS 上游数据未就绪") == []
        reconfirmed = await runtime.decide(memory_id, MemoryDecision.CONFIRM)
        assert reconfirmed is not None and reconfirmed.status is MemoryStatus.CONFIRMED
        assert await runtime.search("LTS 上游数据未就绪")

        counts = await runtime.counts()
        assert counts.pending == 0
        assert counts.confirmed == 1
        assert counts.rejected == 0
        async with factory() as session:
            link_count = await session.scalar(
                select(func.count()).select_from(MemoryEvidenceRecord)
            )
            assert link_count == 3

            # 绕过 Service 写非法状态，证明数据库 CheckConstraint 是最后一道防线。
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "UPDATE case_memories SET status = 'unreviewed' "
                        "WHERE memory_id = :memory_id"
                    ),
                    {"memory_id": memory_id},
                )
                await session.flush()
            await session.rollback()
    finally:
        # 无论断言是否失败都清理合成案例并释放 asyncpg 池，避免影响后续专项测试。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()
