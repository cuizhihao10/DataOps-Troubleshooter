"""验证 confirmed 案例到 GraphRAG 节点/边的纯映射与稳定标识契约。

这些单元测试不连接 PostgreSQL，专注检查向量复用、来源、正文边界、双向边 ID 和输入拒绝；真实
事务、pgvector 邻居选择、幂等 upsert、级联删除与回滚由 PostgreSQL 集成测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.settings import Settings
from app.domain.models import CaseMemory, Component, MemoryStatus
from app.memory.graph_registration import (
    CASE_GRAPH_SOURCE_ID,
    case_graph_node,
    case_graph_node_id,
    case_similarity_edge,
)
from app.memory.models import StoredCaseMemory
from app.retrieval.models import KnowledgeNodeType, KnowledgeRelationType

NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)


def _stored_case(*, long_solution: bool = False) -> StoredCaseMemory:
    """构造一个 confirmed 内部案例快照，并可生成超长方案验证正文裁剪边界。

    返回对象包含八维非零向量、Provider/维度元数据和可追溯证据；数据均为合成内容。该 helper
    不访问数据库，若领域字段或向量契约漂移会在 Pydantic 构造阶段使测试显式失败。
    """

    solution = "在隔离环境复核依赖后再执行补数。"
    if long_solution:
        solution = solution * 500
    memory = CaseMemory(
        memory_id="mem_0123456789abcdef",
        symptoms=["LTS 任务等待上游"],
        root_cause="上游数据未按时就绪",
        fault_path=["LTS 等待 BDS 输出"],
        solution_steps=[solution],
        components=[Component.LTS, Component.BDS],
        tags=["cross_chain", "confirmed"],
        evidence_refs=["ev_case_graph_001"],
        status=MemoryStatus.CONFIRMED,
        occurrence_count=2,
        created_at=NOW,
        updated_at=NOW,
    )
    return StoredCaseMemory(
        memory=memory,
        signature="a" * 64,
        embedding=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        embedding_provider="unit-test-embedding:v1",
        embedding_dimensions=8,
    )


def test_case_graph_node_reuses_audited_memory_embedding_and_source() -> None:
    """确认节点完整复用记忆向量空间，并把结构化字段映射为可检索 case 内容。

    断言覆盖稳定 node_id、类型、来源、可靠性、组件/标签别名和正文关键段落；若实现重新生成向量、
    泄漏管理字段或改变来源语义，测试会失败。该纯映射不会产生 SQL 或外部 Provider 调用。
    """

    stored = _stored_case()
    node = case_graph_node(stored)

    assert node.node_id == "case_0123456789abcdef"
    assert node.node_type is KnowledgeNodeType.CASE
    assert node.source_id == stored.memory.memory_id
    assert node.embedding == stored.embedding
    assert node.embedding_provider == stored.embedding_provider
    assert node.embedding_dimensions == stored.embedding_dimensions
    assert node.reliability == 0.9
    assert node.aliases == ["lts", "bds", "cross_chain", "confirmed"]
    assert "根因：上游数据未按时就绪" in node.content
    assert "证据引用：ev_case_graph_001" in node.content
    assert "不包含模型原始思维链" in node.source_span


def test_case_graph_node_content_respects_graphrag_budget() -> None:
    """确认超长案例正文被确定性裁剪到 KnowledgeNode 的 4000 字符上限。

    根因和症状位于裁剪前部，因而仍可被全文/向量检索使用；长方案只影响尾部。测试证明映射不会
    因合法但很长的案例列表使确认事务在 Pydantic 节点边界意外失败。
    """

    node = case_graph_node(_stored_case(long_solution=True))

    assert len(node.content) == 4000
    assert node.content.startswith("症状：LTS 任务等待上游\n根因：上游数据未按时就绪")


def test_case_similarity_edges_are_directional_stable_and_symmetric_by_pair() -> None:
    """确认同一方向重复构造得到稳定 ID，而反向关系得到不同 ID 和相同权重。

    PostgreSQL 边是有向的，所以业务上的对称相似关系由两条方向边表达；测试同时锁定来源版本、
    关系类型和可解释 source_span。自环或零权重由构造函数显式拒绝，不会进入数据库。
    """

    forward = case_similarity_edge(
        "case_0123456789abcdef",
        "case_fedcba9876543210",
        similarity=0.875,
    )
    replay = case_similarity_edge(
        "case_0123456789abcdef",
        "case_fedcba9876543210",
        similarity=0.875,
    )
    reverse = case_similarity_edge(
        "case_fedcba9876543210",
        "case_0123456789abcdef",
        similarity=0.875,
    )

    assert forward.edge_id == replay.edge_id
    assert forward.edge_id != reverse.edge_id
    assert forward.relation_type is KnowledgeRelationType.SIMILAR_TO
    assert forward.source_id == CASE_GRAPH_SOURCE_ID
    assert forward.weight == reverse.weight == 0.875
    assert "cosine similarity=0.875000" in forward.source_span


def test_case_graph_identifiers_reject_untrusted_or_invalid_inputs() -> None:
    """确认任意 memory_id、自环和非正相似度不能生成图主键或关系。

    这些失败发生在 SQL 前：ID 必须严格匹配 ``mem_<16 lowercase hex>``，边两端必须不同且权重
    位于 ``(0, 1]``。测试防止 API 文本被拼入数据库标识，也锁定数据库 CheckConstraint 前的领域
    防线。
    """

    with pytest.raises(ValueError, match="mem_<16 hex>"):
        case_graph_node_id("case_not_a_memory")
    with pytest.raises(ValueError, match="lowercase hexadecimal"):
        case_graph_node_id("mem_0123456789ABCDEZ")
    with pytest.raises(ValueError, match="self-loop"):
        case_similarity_edge("case_a", "case_a", similarity=0.8)
    with pytest.raises(ValueError, match="greater than zero"):
        case_similarity_edge("case_a", "case_b", similarity=0.0)


def test_case_graph_threshold_must_cover_non_duplicate_similarity_band() -> None:
    """确认集中配置拒绝高于记忆去重阈值的图关系阈值。

    图阈值的目标是连接未被 canonical 去重合并的相似案例；若配置为 0.95 而去重阈值为 0.90，
    该区间为空且设计语义矛盾。Settings 在启动时失败，避免部署后只有 confirm 副作用却永远难以
    形成相似边。
    """

    with pytest.raises(ValueError, match="must not exceed memory dedup threshold"):
        Settings(
            _env_file=None,
            memory_dedup_similarity_threshold=0.9,
            case_graph_similarity_threshold=0.95,
        )
