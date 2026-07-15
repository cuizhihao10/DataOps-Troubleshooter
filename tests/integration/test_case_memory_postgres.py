"""验证案例记忆迁移、pgvector 去重、run 幂等、状态决策和 confirmed 召回。

测试使用本地 PostgreSQL/pgvector 和真实 asyncpg/SQLAlchemy 仓储，不访问模型 API；Embedding 使用
确定性 Provider。它覆盖从 accepted ReportRunResult 到 pending/confirmed/rejected 的最小闭环。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
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
from app.memory.graph_registration import CASE_GRAPH_SOURCE_ID, case_graph_node_id
from app.memory.models import (
    MemoryDecision,
    MemoryDuplicateType,
    MemoryRetrievalChannel,
    MemoryStageStatus,
)
from app.memory.runtime import PostgresMemoryRuntime
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    ReportEventType,
    ReportPublicEvent,
    ReportRunResult,
    ReportWorkflowOutcome,
)
from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.models import (
    CaseMemoryRecord,
    KnowledgeEdgeRecord,
    KnowledgeNodeRecord,
    MemoryEvidenceRecord,
)
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.retrieval.models import KnowledgeNodeType, KnowledgeRelationType, RetrievalMode
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.service import GraphRetrievalService

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


class ConstantEmbeddingProvider:
    """为图注册集成测试返回同一八维方向，稳定制造 similarity=1 的合成空间。

    不同组件案例不会被记忆去重仓储互相比较，因此仍会保留为两条记录；图注册不按组件过滤，
    会把它们识别为相似节点。该替身只用于本地测试，不访问网络或冒充模型级语义质量。
    """

    @property
    def provider_id(self) -> str:
        """返回测试专用稳定空间 ID，使查询不会混入默认知识种子向量。

        ID 在整个测试生命周期保持不变；数据库通过它隔离其他 Provider。该属性不执行 I/O，若
        实现改名，已有测试数据将无法被同一 GraphRetrievalService 召回并使断言失败。
        """

        return "constant-case-test:v1"

    @property
    def dimensions(self) -> int:
        """返回固定八维下限向量长度，与 pgvector/Pydantic 约束一致。

        所有 ``embed_texts`` 结果都严格使用该维度；调用方若观察到长度漂移会在写数据库前失败。
        属性不依赖输入或可变状态。
        """

        return 8

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """为每个非空文本返回相同单位向量，并保持输入顺序和批次数量。

        空白文本显式失败，避免替身绕过生产 Provider 的基本契约。相同方向确保 cosine similarity
        精确为一，从而让测试只验证图注册/扩展，而不依赖 feature hashing 的偶然碰撞。
        """

        if any(not item.strip() for item in texts):
            raise ValueError("constant test embedding input must not be blank")
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]


class DirectedGraphEmbeddingProvider:
    """用二维单位方向嵌入八维数组，制造可证明图邻居改变 top-k 的测试几何关系。

    查询方向为 0°；案例 A/B/C 分别为 30°、-45°、60°。直接排序是 A>B>C，但 A→C 的
    ``0.866 * 0.866 = 0.75`` 图传播分高于 B 的约 0.707，使 C 在 limit=2 时替换 B。所有案例间
    相似度低于 0.99 去重阈值，测试不会因 staging 合并而失去三个独立节点。
    """

    @property
    def provider_id(self) -> str:
        """返回与其他测试/知识种子隔离的稳定向量空间标识。

        PostgreSQL 查询必须同时匹配该 ID 和八维长度；属性不执行 I/O，也不随文本变化。若生产
        仓储忽略 Provider 过滤，本测试会混入默认知识节点并使候选顺序断言失败。
        """

        return "directed-case-graph-test:v1"

    @property
    def dimensions(self) -> int:
        """返回满足生产最低维度约束的固定八维长度。

        实际几何只使用前两维，其余补零；这样仍由真实 pgvector cosine 执行，而不是在 Python 中
        伪造数据库分数。
        """

        return 8

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """按文本中的合成标识选择单位方向，并保持批次顺序和数量。

        ``图案例 A/B/C`` 映射到三个案例方向，``graph-neighbor-query`` 映射到查询方向；其他非空
        文本使用查询方向以支持测试流程中的普通搜索。空白输入显式失败，不访问模型或网络。
        """

        vectors: list[list[float]] = []
        for item in texts:
            if not item.strip():
                raise ValueError("directed test embedding input must not be blank")
            if "图案例 A" in item:
                vector = [0.8660254038, 0.5]
            elif "图案例 B" in item:
                vector = [0.7071067812, -0.7071067812]
            elif "图案例 C" in item:
                vector = [0.5, 0.8660254038]
            else:
                vector = [1.0, 0.0]
            vectors.append([*vector, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        return vectors


def _accepted_result(
    *,
    run_id: str,
    evidence_id: str,
    root_cause: str = "上游数据未按时就绪",
    observed_at: datetime = NOW,
    component: Component = Component.LTS,
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
                symptom=f"{component.value.upper()} 合成故障现象",
                candidate_root_cause=root_cause,
                components=[component],
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
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
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
        async with factory() as session:
            graph_node = await session.scalar(
                select(KnowledgeNodeRecord).where(
                    KnowledgeNodeRecord.node_id == case_graph_node_id(memory_id)
                )
            )
            assert graph_node is not None
            assert graph_node.node_type == KnowledgeNodeType.CASE.value
            assert graph_node.embedding_provider == "deterministic-hash:v1"
            assert len(list(graph_node.embedding)) == 128
            assert any(value != 0 for value in graph_node.embedding)
        matches = await runtime.search("LTS 上游数据未就绪")
        assert matches and matches[0].memory.memory_id == memory_id
        assert matches[0].memory.status is MemoryStatus.CONFIRMED
        assert 0 <= matches[0].similarity <= 1

        rejected = await runtime.decide(memory_id, MemoryDecision.REJECT)
        assert rejected is not None and rejected.status is MemoryStatus.REJECTED
        async with factory() as session:
            assert await session.get(KnowledgeNodeRecord, case_graph_node_id(memory_id)) is None
        assert await runtime.search("LTS 上游数据未就绪") == []
        reconfirmed = await runtime.decide(memory_id, MemoryDecision.CONFIRM)
        assert reconfirmed is not None and reconfirmed.status is MemoryStatus.CONFIRMED
        await runtime.decide(memory_id, MemoryDecision.CONFIRM)
        async with factory() as session:
            registered_count = await session.scalar(
                select(func.count())
                .select_from(KnowledgeNodeRecord)
                .where(KnowledgeNodeRecord.source_id == memory_id)
            )
            assert registered_count == 1
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
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_confirmed_cases_create_symmetric_edges_and_graphrag_paths() -> None:
    """验证两个 confirmed 案例注册节点、稳定双向边，并被真实 GraphRAG 向量/路径召回。

    两个案例使用不同组件绕开组件内去重，但共享测试向量空间，因此相似度为一。重复 confirm 不增
    加边；reject 删除节点并级联清除两方向关系。测试使用真实 PostgreSQL、pgvector 和递归扩图，
    不访问外部模型或生产数据。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    provider = ConstantEmbeddingProvider()
    runtime = PostgresMemoryRuntime(
        factory,
        provider,
        dedup_similarity_threshold=0.99,
        graph_similarity_threshold=0.8,
        default_search_limit=5,
    )
    try:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))

        first = await runtime.stage(
            _accepted_result(
                run_id="run_case_graph_pg_001",
                evidence_id="ev_case_graph_pg_001",
                root_cause="LTS 上游分区未就绪",
                component=Component.LTS,
            )
        )
        second = await runtime.stage(
            _accepted_result(
                run_id="run_case_graph_pg_002",
                evidence_id="ev_case_graph_pg_002",
                root_cause="BDS 计算资源队列拥塞",
                component=Component.BDS,
                observed_at=NOW + timedelta(minutes=1),
            )
        )
        assert first.memory is not None and second.memory is not None
        first_id = first.memory.memory_id
        second_id = second.memory.memory_id

        await runtime.decide(first_id, MemoryDecision.CONFIRM)
        await runtime.decide(second_id, MemoryDecision.CONFIRM)
        await runtime.decide(second_id, MemoryDecision.CONFIRM)

        async with factory() as session:
            nodes = (
                await session.scalars(
                    select(KnowledgeNodeRecord)
                    .where(KnowledgeNodeRecord.source_id.in_([first_id, second_id]))
                    .order_by(KnowledgeNodeRecord.node_id)
                )
            ).all()
            edges = (
                await session.scalars(
                    select(KnowledgeEdgeRecord)
                    .where(KnowledgeEdgeRecord.source_id == CASE_GRAPH_SOURCE_ID)
                    .order_by(KnowledgeEdgeRecord.edge_id)
                )
            ).all()
            assert len(nodes) == 2
            assert len(edges) == 2
            assert {edge.relation_type for edge in edges} == {
                KnowledgeRelationType.SIMILAR_TO.value
            }
            assert {edge.weight for edge in edges} == {1.0}
            assert {(edge.from_node_id, edge.to_node_id) for edge in edges} == {
                (case_graph_node_id(first_id), case_graph_node_id(second_id)),
                (case_graph_node_id(second_id), case_graph_node_id(first_id)),
            }

            retrieval = await GraphRetrievalService(
                PostgresGraphRepository(session),
                provider,
            ).retrieve(
                "查询相似已确认案例",
                seed_limit=5,
                max_hops=1,
                mode=RetrievalMode.HYBRID_GRAPH,
            )
            assert {seed.node.node_id for seed in retrieval.seeds} == {
                case_graph_node_id(first_id),
                case_graph_node_id(second_id),
            }
            assert any(
                path.edges[0].relation_type is KnowledgeRelationType.SIMILAR_TO
                for path in retrieval.paths
            )

        await runtime.decide(first_id, MemoryDecision.REJECT)
        async with factory() as session:
            assert await session.get(KnowledgeNodeRecord, case_graph_node_id(first_id)) is None
            owned_edges = await session.scalar(
                select(func.count())
                .select_from(KnowledgeEdgeRecord)
                .where(KnowledgeEdgeRecord.source_id == CASE_GRAPH_SOURCE_ID)
            )
            assert owned_edges == 0
    finally:
        # 动态 case 节点先删以级联清边，再清审计关联和案例；最后释放连接池。
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_graph_node_collision_rolls_back_memory_confirmation() -> None:
    """验证稳定节点 ID 被其他来源占用时，confirm 状态与图写入在同一事务回滚。

    测试先暂存 pending 案例，再人工插入同 ID 的合成知识节点模拟来源冲突。runtime 必须抛错且案例
    仍为 pending；这证明 API 不会看到“确认成功但图未注册”的部分状态，也不会覆盖人工种子。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    runtime = PostgresMemoryRuntime(
        factory,
        DeterministicHashEmbeddingProvider(dimensions=128),
        dedup_similarity_threshold=0.92,
        graph_similarity_threshold=0.75,
        default_search_limit=5,
    )
    try:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))

        staged = await runtime.stage(
            _accepted_result(
                run_id="run_case_graph_collision_001",
                evidence_id="ev_case_graph_collision_001",
            )
        )
        assert staged.memory is not None
        memory_id = staged.memory.memory_id
        conflicting_id = case_graph_node_id(memory_id)
        async with factory.begin() as session:
            session.add(
                KnowledgeNodeRecord(
                    node_id=conflicting_id,
                    node_type=KnowledgeNodeType.CASE.value,
                    name="合成冲突节点",
                    content="仅用于验证来源冲突回滚。",
                    aliases=[],
                    source_id="manual-test-source",
                    source_span="合成测试数据，不代表生产知识。",
                    reliability=0.5,
                )
            )

        with pytest.raises(ValueError, match="node ID collision"):
            await runtime.decide(memory_id, MemoryDecision.CONFIRM)

        async with factory() as session:
            record = await session.get(CaseMemoryRecord, memory_id)
            assert record is not None and record.status == MemoryStatus.PENDING.value
            conflict = await session.get(KnowledgeNodeRecord, conflicting_id)
            assert conflict is not None and conflict.source_id == "manual-test-source"
    finally:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_id LIKE 'case_%' AND "
                    "(source_id LIKE 'mem_%' OR source_id = 'manual-test-source')"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_search_merges_graph_neighbor_and_reject_removes_it() -> None:
    """验证真实 pgvector top-k 与 SIMILAR_TO 邻居合并后，图候选改变最终搜索结果。

    三个同组件 confirmed 案例使用受控方向：直接 top-2 为 A/B，A→C 图传播分高于 B，因此最终
    返回 A/C，且 C 是 graph-only 并携带稳定 edge 引用。reject C 后节点/边级联删除，下一次搜索
    恢复 A/B，证明状态过滤和图清理即时生效。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    provider = DirectedGraphEmbeddingProvider()
    runtime = PostgresMemoryRuntime(
        factory,
        provider,
        dedup_similarity_threshold=0.99,
        graph_similarity_threshold=0.8,
        default_search_limit=5,
    )
    try:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))

        staged = []
        for index, label in enumerate(("A", "B", "C"), start=1):
            result = await runtime.stage(
                _accepted_result(
                    run_id=f"run_graph_recall_pg_00{index}",
                    evidence_id=f"ev_graph_recall_pg_00{index}",
                    root_cause=f"图案例 {label}",
                    component=Component.LTS,
                    observed_at=NOW + timedelta(minutes=index),
                )
            )
            assert result.memory is not None
            staged.append(result.memory)
            await runtime.decide(result.memory.memory_id, MemoryDecision.CONFIRM)

        matches = await runtime.search("graph-neighbor-query", limit=2)
        assert [match.memory.root_cause for match in matches] == ["图案例 A", "图案例 C"]
        assert matches[0].retrieval_channels == [MemoryRetrievalChannel.VECTOR]
        assert matches[0].direct_similarity == pytest.approx(0.8660254, abs=1e-5)
        graph_match = matches[1]
        assert graph_match.retrieval_channels == [MemoryRetrievalChannel.GRAPH]
        assert graph_match.direct_similarity is None
        assert graph_match.graph_score == pytest.approx(0.75, abs=1e-5)
        assert len(graph_match.graph_edge_refs) == 1
        assert graph_match.graph_edge_refs[0].startswith("edge_case_similar_")

        case_c = next(memory for memory in staged if memory.root_cause == "图案例 C")
        rejected = await runtime.decide(case_c.memory_id, MemoryDecision.REJECT)
        assert rejected is not None and rejected.status is MemoryStatus.REJECTED
        after_reject = await runtime.search("graph-neighbor-query", limit=2)
        assert [match.memory.root_cause for match in after_reject] == ["图案例 A", "图案例 B"]
        assert all(
            MemoryRetrievalChannel.GRAPH not in match.retrieval_channels for match in after_reject
        )
    finally:
        # 先删除动态节点级联清边，再清关联和案例，保证失败中断也不污染后续 PostgreSQL 专项。
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_delete_removes_case_evidence_and_graph_node_atomically() -> None:
    """验证永久删除在真实 PostgreSQL 中清理案例主表、证据关联和动态 GraphRAG 节点。

    案例先经过 stage/confirm，确保确实存在可删除的 case 节点；随后 runtime.delete
    在单一事务内清理全部资源，最终通过独立只读会话确认三个表都不存在该 memory_id。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    runtime = PostgresMemoryRuntime(
        factory,
        ConstantEmbeddingProvider(),
        dedup_similarity_threshold=0.99,
        graph_similarity_threshold=0.8,
        default_search_limit=5,
    )
    try:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))

        staged = await runtime.stage(
            _accepted_result(
                run_id="run_memory_delete_pg_001",
                evidence_id="ev_memory_delete_pg_001",
                root_cause="永久删除合成案例",
                component=Component.LTS,
                observed_at=NOW,
            )
        )
        assert staged.memory is not None
        confirmed = await runtime.decide(staged.memory.memory_id, MemoryDecision.CONFIRM)
        assert confirmed is not None and confirmed.status is MemoryStatus.CONFIRMED

        deleted = await runtime.delete(staged.memory.memory_id)
        assert deleted is not None and deleted.memory_id == staged.memory.memory_id
        async with factory() as session:
            assert await session.get(CaseMemoryRecord, staged.memory.memory_id) is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEvidenceRecord)
                    .where(MemoryEvidenceRecord.memory_id == staged.memory.memory_id)
                )
                == 0
            )
            assert (
                await session.get(KnowledgeNodeRecord, case_graph_node_id(staged.memory.memory_id))
                is None
            )
    finally:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM knowledge_nodes "
                    "WHERE node_type = 'case' AND source_id LIKE 'mem_%'"
                )
            )
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()
