"""在真实 PostgreSQL/pgvector 上运行长期记忆 vector-only 与 vector+graph 召回评测。

测试从版本化 JSON 建立五条合成案例，通过生产仓储写入并用 runtime confirm/reject 注册图状态，
再运行 ``memory-recall-eval:v1`` 三条查询。确定性角度 Provider 只控制可复现几何，不替代真实
pgvector cosine、SQL join、SIMILAR_TO 边、confirmed 过滤或最终合并排序。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import cos, radians, sin
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.domain.models import CaseMemory, MemoryStatus
from app.memory.evaluation import (
    MemoryRecallEvalSuite,
    evaluate_memory_recall,
    load_memory_recall_eval_suite,
)
from app.memory.graph_registration import CASE_GRAPH_SOURCE_ID, case_graph_node_id
from app.memory.models import MemoryDecision, StoredCaseMemory
from app.memory.repository import PostgresCaseMemoryRepository
from app.memory.runtime import PostgresMemoryRuntime
from app.memory.service import memory_signature
from app.persistence.database import create_database_engine, create_session_factory
from app.persistence.models import KnowledgeEdgeRecord, KnowledgeNodeRecord

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
SUITE_PATH = Path("data/evals/memory_recall_cases.json")
# 使用明确过去的 UTC 时间，避免测试机/容器当前日期早于合成 created_at 时，数据库 now() 状态更新
# 被领域约束正确识别为“updated_at 早于 created_at”。时间值不参与召回几何或评测指标。
NOW = datetime(2020, 1, 1, 10, 0, tzinfo=UTC)


class AngleMemoryEmbeddingProvider:
    """按 suite embedding_key 返回二维单位方向补零后的八维确定性向量。

    corpus root cause 和 query 各自映射到角度键；数据库仍真实计算 cosine。30°/60° 案例达到 0.866
    图阈值，0° 查询下 C 的传播分 0.75 超过 B 的 0.707，形成可证明的 graph-only 救回。未知文本
    显式失败，避免测试 Provider 用默认向量掩盖 fixture 漂移。
    """

    def __init__(self, suite: MemoryRecallEvalSuite) -> None:
        """从已校验 suite 建立 root/query 到角度键的不可变查找映射。

        构造不生成向量或访问数据库；root/query 在 suite 中已受长度和唯一性约束，因此字典不会
        静默覆盖两条不同案例的检索向量配置。
        """

        self._root_keys = {item.root_cause: item.embedding_key for item in suite.corpus}
        self._query_keys = {item.query: item.query_embedding_key for item in suite.cases}

    @property
    def provider_id(self) -> str:
        """返回评测专用稳定 Provider ID，隔离默认知识种子与其他测试向量。

        ID 不随案例变化；pgvector 查询同时匹配该 ID 和维度，若生产代码遗漏空间过滤，专项结果会
        混入其他数据并使标签断言失败。
        """

        return "memory-recall-angle-eval:v1"

    @property
    def dimensions(self) -> int:
        """返回满足生产 Pydantic/pgvector 下限的固定八维向量长度。

        角度几何只占前两维，其余补零；所有文本和批次始终返回相同长度，不允许运行时发生漂移。
        """

        return 8

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序解析 corpus root 或完整 query，并返回对应单位向量。

        空白、未知文本或无法匹配唯一键时抛 ValueError，不返回部分结果。该方法无网络 I/O；真实
        Provider 可通过同一协议替换，但本评测数字届时必须重跑而不能沿用。
        """

        vectors: list[list[float]] = []
        for value in texts:
            if not value.strip():
                raise ValueError("memory recall eval embedding text must not be blank")
            key = self._query_keys.get(value)
            if key is None:
                matched_keys = [
                    embedding_key
                    for root_cause, embedding_key in self._root_keys.items()
                    if root_cause in value
                ]
                if len(matched_keys) != 1:
                    raise ValueError(
                        f"memory recall eval text has no unique embedding key: {value}"
                    )
                key = matched_keys[0]
            vectors.append(_angle_vector(key))
        return vectors


def _angle_vector(key: str) -> list[float]:
    """把 ``angle_<degrees>`` 转成前两维单位方向并补零到八维。

    角度来自已校验 fixture，但函数仍检查前缀和整数范围；cos/sin 的浮点结果由 pgvector cosine
    处理，补零不改变方向。非法键在数据库写入前失败。
    """

    if not key.startswith("angle_"):
        raise ValueError("memory recall embedding key must start with angle_")
    degrees = int(key.removeprefix("angle_"))
    if not 0 <= degrees < 360:
        raise ValueError("memory recall embedding angle must be within [0, 360)")
    angle = radians(degrees)
    return [cos(angle), sin(angle), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _memory_id(label: str) -> str:
    """根据 suite label 生成符合 Registrar 格式的稳定 ``mem_<16hex>`` ID。

    SHA-256 仅用于可重放标识，不承担凭据或安全签名用途；不同标签在本小型 corpus 中得到稳定 ID，
    完整 label 仍保留在评测报告映射中。
    """

    return f"mem_{sha256(label.encode()).hexdigest()[:16]}"


async def _insert_eval_corpus(
    suite: MemoryRecallEvalSuite,
    provider: AngleMemoryEmbeddingProvider,
    factory,
    runtime: PostgresMemoryRuntime,
) -> None:
    """用生产仓储插入 pending corpus，再通过 runtime 显式 confirm/reject 建立真实图状态。

    主记录和 evidence 关联先在一个事务写入；随后逐条用户决策触发 production 图注册/清理。confirmed
    案例生成 case 节点和相似边，rejected 案例保留主记录但无图节点。任一步失败会传播并由测试
    finally 清理，不伪造部分 suite。
    """

    async with factory.begin() as session:
        repository = PostgresCaseMemoryRepository(session)
        for index, item in enumerate(suite.corpus, start=1):
            timestamp = NOW + timedelta(minutes=index)
            memory = CaseMemory(
                memory_id=_memory_id(item.label),
                symptoms=[f"合成召回症状：{item.label}"],
                root_cause=item.root_cause,
                fault_path=["合成只读链路"],
                solution_steps=["仅在隔离环境人工复核。"],
                components=[item.component],
                tags=[item.label, "memory_recall_eval"],
                evidence_refs=[f"ev_{item.label}"],
                status=MemoryStatus.PENDING,
                occurrence_count=1,
                created_at=timestamp,
                updated_at=timestamp,
            )
            embedding = (await provider.embed_texts([item.root_cause]))[0]
            stored = StoredCaseMemory(
                memory=memory,
                signature=memory_signature(memory.components, memory.root_cause),
                embedding=embedding,
                embedding_provider=provider.provider_id,
                embedding_dimensions=provider.dimensions,
            )
            await repository.insert(stored, source_run_id=f"run_{item.label}")

    # 决策必须走 runtime 而不是直接改状态，否则无法证明 confirmed 图注册和 reject 清理参与评测。
    for item in suite.corpus:
        decision = (
            MemoryDecision.CONFIRM
            if item.status is MemoryStatus.CONFIRMED
            else MemoryDecision.REJECT
        )
        decided = await runtime.decide(_memory_id(item.label), decision)
        if decided is None or decided.status is not item.status:
            raise RuntimeError(f"memory recall eval decision failed for {item.label}")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_memory_recall_ablation_matches_versioned_measured_report() -> None:
    """运行三条真实召回案例并验证图增益、基线持平、reject 隔离和图存储事实。

    vector-only macro Recall/Precision 应为 5/6，vector+graph 为 1，差值 1/6；graph rescue 中 C 必须
    graph-only，三个案例两模式均不得命中 rejected E。数据库应仅有 A↔C 两条动态相似边，E 没有
    case 节点。所有断言仅适用于固定合成 suite 与角度 Provider。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    suite = load_memory_recall_eval_suite(SUITE_PATH)
    provider = AngleMemoryEmbeddingProvider(suite)
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
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

        await _insert_eval_corpus(suite, provider, factory, runtime)
        report = await evaluate_memory_recall(suite, runtime)

        assert report.metric_kind == "measured"
        assert report.vector_only_macro_recall == pytest.approx(5 / 6)
        assert report.vector_graph_macro_recall == 1
        assert report.recall_delta == pytest.approx(1 / 6)
        assert report.vector_only_macro_precision == pytest.approx(5 / 6)
        assert report.vector_graph_macro_precision == 1
        assert report.precision_delta == pytest.approx(1 / 6)
        assert report.forbidden_hit_count == 0
        assert report.case_reports[0].graph_rescued_labels == ["memory_case_c"]
        assert report.case_reports[0].vector_graph.graph_only_hits == ["memory_case_c"]
        assert all(not item.regressed_labels for item in report.case_reports)

        async with factory() as session:
            edge_count = await session.scalar(
                select(func.count())
                .select_from(KnowledgeEdgeRecord)
                .where(KnowledgeEdgeRecord.source_id == CASE_GRAPH_SOURCE_ID)
            )
            assert edge_count == 2
            rejected_node = await session.get(
                KnowledgeNodeRecord,
                case_graph_node_id(_memory_id("memory_case_e")),
            )
            assert rejected_node is None
    finally:
        # 动态节点先删以级联清边，再清 evidence/case 主记录并释放 asyncpg 池，保证专项可重复运行。
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
