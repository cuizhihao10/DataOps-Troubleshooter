"""验证历史案例向量直接命中与 SIMILAR_TO 图邻居的确定性合并契约。

测试不连接 PostgreSQL，而是直接构造 confirmed 案例、向量命中和图传播候选，检查通道字段、
去重、评分、稳定 edge 引用和排序。真实 join、状态 SQL 过滤与 reject 清理由 PostgreSQL 测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.models import CaseMemory, Component, MemoryStatus
from app.memory.models import CaseMemoryMatch, MemoryRetrievalChannel
from app.memory.repository import merge_case_memory_matches

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def _memory(
    memory_id: str,
    *,
    updated_offset: int = 0,
    status: MemoryStatus = MemoryStatus.CONFIRMED,
) -> CaseMemory:
    """构造一个可区分新鲜度和状态的合成案例，用于纯合并排序测试。

    ``updated_offset`` 只在最终分完全相同时提供稳定次级排序；memory_id、根因和证据均为脱敏合成
    值。若状态为 rejected，CaseMemory 本身仍合法，但 CaseMemoryMatch/merge 必须拒绝其进入默认
    召回。
    """

    return CaseMemory(
        memory_id=memory_id,
        symptoms=[f"合成症状 {memory_id}"],
        root_cause=f"合成根因 {memory_id}",
        components=[Component.LTS],
        evidence_refs=[f"ev_{memory_id}"],
        status=status,
        occurrence_count=1,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=updated_offset),
    )


def _vector_match(memory_id: str, similarity: float) -> CaseMemoryMatch:
    """构造一个只由 pgvector 直接命中的强类型历史候选。

    最终分与 direct_similarity 相同，retrieval_channels 只包含 vector；该 helper 不伪造图引用。
    分数越界或状态错误会由生产 Pydantic 模型立即暴露。
    """

    return CaseMemoryMatch(
        memory=_memory(memory_id),
        similarity=similarity,
        retrieval_channels=[MemoryRetrievalChannel.VECTOR],
        direct_similarity=similarity,
    )


def test_graph_only_neighbor_can_displace_weaker_direct_candidate() -> None:
    """验证图传播分高于较弱直接分时，图邻居真实进入最终 top-k。

    A/B 是直接 top-2，C 由 A 的 SIMILAR_TO 边得到 0.75 图分；C 应替换直接分 0.70 的 B。结果
    保留 C 的 graph-only 通道和 edge 引用，证明图关系改变候选而非只作为旁路展示。
    """

    merged = merge_case_memory_matches(
        [_vector_match("mem_aaaaaaaaaaaaaaaa", 0.86), _vector_match("mem_bbbbbbbbbbbbbbbb", 0.70)],
        [
            (
                _memory("mem_cccccccccccccccc"),
                0.75,
                "edge_case_similar_1111111111111111",
            )
        ],
        limit=2,
    )

    assert [match.memory.memory_id for match in merged] == [
        "mem_aaaaaaaaaaaaaaaa",
        "mem_cccccccccccccccc",
    ]
    graph_match = merged[1]
    assert graph_match.retrieval_channels == [MemoryRetrievalChannel.GRAPH]
    assert graph_match.direct_similarity is None
    assert graph_match.graph_score == 0.75
    assert graph_match.graph_edge_refs == ["edge_case_similar_1111111111111111"]


def test_same_case_merges_vector_and_best_graph_routes_without_double_counting() -> None:
    """验证同一案例同时直接/图命中时只返回一次，并保留最高图分的并列 edge 引用。

    graph 0.82 高于 direct 0.80，因此最终分取 0.82；较弱 0.70 路径被丢弃，两个并列 0.82 路径按
    edge ID 排序保留。该行为避免一个案例因多条边重复占用 top-k，同时仍保留可审计来源。
    """

    memory = _memory("mem_dddddddddddddddd")
    merged = merge_case_memory_matches(
        [
            CaseMemoryMatch(
                memory=memory,
                similarity=0.80,
                retrieval_channels=[MemoryRetrievalChannel.VECTOR],
                direct_similarity=0.80,
            )
        ],
        [
            (memory, 0.70, "edge_case_similar_3333333333333333"),
            (memory, 0.82, "edge_case_similar_2222222222222222"),
            (memory, 0.82, "edge_case_similar_1111111111111111"),
        ],
        limit=5,
    )

    assert len(merged) == 1
    match = merged[0]
    assert match.similarity == 0.82
    assert match.retrieval_channels == [
        MemoryRetrievalChannel.VECTOR,
        MemoryRetrievalChannel.GRAPH,
    ]
    assert match.direct_similarity == 0.80
    assert match.graph_score == 0.82
    assert match.graph_edge_refs == [
        "edge_case_similar_1111111111111111",
        "edge_case_similar_2222222222222222",
    ]


def test_match_contract_rejects_channel_score_or_edge_inconsistency() -> None:
    """验证 raw match 不能声明图通道却缺少传播分/边，或返回与分量不一致的最终分。

    这些错误必须在 API/Planner 前由 Pydantic 拒绝；否则调用方无法判断 similarity 来自哪条检索
    路径。测试同时锁定稳定 edge ID 前缀，避免任意数据库字符串冒充案例相似关系引用。
    """

    memory = _memory("mem_eeeeeeeeeeeeeeee")
    with pytest.raises(ValidationError, match="graph retrieval channel must match graph_score"):
        CaseMemoryMatch(
            memory=memory,
            similarity=0.7,
            retrieval_channels=[MemoryRetrievalChannel.GRAPH],
            graph_edge_refs=["edge_case_similar_1111111111111111"],
        )
    with pytest.raises(ValidationError, match="stable case similarity IDs"):
        CaseMemoryMatch(
            memory=memory,
            similarity=0.7,
            retrieval_channels=[MemoryRetrievalChannel.GRAPH],
            graph_score=0.7,
            graph_edge_refs=["edge_untrusted"],
        )
    with pytest.raises(ValidationError, match="strongest retrieval score"):
        CaseMemoryMatch(
            memory=memory,
            similarity=0.9,
            retrieval_channels=[MemoryRetrievalChannel.VECTOR],
            direct_similarity=0.7,
        )


def test_merge_rejects_non_confirmed_graph_neighbor() -> None:
    """验证 rejected 图邻居即使携带高分和合法 edge ID也不能进入默认历史候选。

    SQL 会先过滤状态，本纯函数仍提供第二道领域防线；若数据库快照或测试替身漂移，合并直接失败
    而不是静默泄漏已取消确认案例。
    """

    with pytest.raises(ValueError, match="must be confirmed"):
        merge_case_memory_matches(
            [_vector_match("mem_ffffffffffffffff", 0.8)],
            [
                (
                    _memory(
                        "mem_9999999999999999",
                        status=MemoryStatus.REJECTED,
                    ),
                    0.9,
                    "edge_case_similar_9999999999999999",
                )
            ],
            limit=2,
        )
