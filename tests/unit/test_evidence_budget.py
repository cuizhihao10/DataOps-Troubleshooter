"""验证 GraphRAG Evidence Bundle 的精确字节预算、路径原子性和省略诊断信息。

测试使用完整 Pydantic 检索模型构造一条合成路径，不依赖数据库；它确保构建器不会超过 UTF-8
上下文预算，不会只纳入路径的一部分节点，并为所有未选候选保留稳定 omitted ID。
"""

import json

from app.retrieval.budget import build_evidence_bundle
from app.retrieval.models import (
    EvidenceBundleBudget,
    GraphRetrievalResult,
    HybridScoringWeights,
    HybridSeedMatch,
    KnowledgeEdge,
    KnowledgeNode,
    RetrievalChannel,
    RetrievalMode,
    ScoredGraphPath,
)


def _retrieval_result() -> GraphRetrievalResult:
    """构造一个双节点一跳的 hybrid_graph 结果，包含种子和可引用完整路径。

    节点正文包含中文以验证 UTF-8 多字节计算，路径保留 edge source_span 和独立 hybrid_score；
    原始 embedding 为空，符合真实检索结果不会把派生向量注入上下文的边界。
    """

    seed_node = KnowledgeNode(
        node_id="symptom_demo_backlog",
        node_type="symptom",
        name="合成积压",
        content="合成同步吞吐下降并出现待处理记录。",
        source_id="synthetic_budget_source",
        source_span="同步吞吐下降并出现待处理记录",
        reliability=1.0,
    )
    root_node = KnowledgeNode(
        node_id="root_cause_demo_conflict",
        node_type="root_cause",
        name="合成主键冲突",
        content="目标端重复键导致同步批次暂停。",
        source_id="synthetic_budget_source",
        source_span="重复键导致同步批次暂停",
        reliability=0.95,
    )
    edge = KnowledgeEdge(
        edge_id="edge_demo_backlog_conflict",
        from_node_id=seed_node.node_id,
        to_node_id=root_node.node_id,
        relation_type="CAUSED_BY",
        weight=1.0,
        source_id="synthetic_budget_source",
        source_span="合成积压由主键冲突导致",
    )
    seed = HybridSeedMatch(
        node=seed_node,
        channels=[RetrievalChannel.LEXICAL, RetrievalChannel.VECTOR],
        semantic_score=0.9,
        lexical_score=0.7,
        reliability_score=1.0,
        freshness_score=0.0,
        hybrid_score=0.575,
    )
    path = ScoredGraphPath(
        path_id="path_0123456789abcdef",
        nodes=[seed_node, root_node],
        edges=[edge],
        depth=1,
        score=1.0,
        source_ids=["synthetic_budget_source"],
        seed_node_id=seed_node.node_id,
        channels=seed.channels,
        semantic_score=seed.semantic_score,
        lexical_score=seed.lexical_score,
        reliability_score=seed.reliability_score,
        freshness_score=seed.freshness_score,
        hybrid_score=0.825,
    )
    return GraphRetrievalResult(
        query="合成同步积压",
        mode=RetrievalMode.HYBRID_GRAPH,
        embedding_provider="unit-provider:v1",
        score_weights=HybridScoringWeights(),
        seeds=[seed],
        paths=[path],
    )


def _selected_payload_size(bundle) -> int:
    """按生产构建器相同的规范 JSON 规则计算测试 Bundle 主体 UTF-8 字节数。

    测试独立重算而不调用私有实现，能够发现 `used_bytes` 只写固定值或遗漏中文多字节的回归；
    omitted IDs 和预算诊断元数据按契约不计入上下文主体。
    """

    payload = {
        "selected_nodes": [node.model_dump(mode="json") for node in bundle.selected_nodes],
        "selected_paths": [path.model_dump(mode="json") for path in bundle.selected_paths],
    }
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def test_generous_budget_selects_complete_path_and_exact_utf8_size() -> None:
    """验证充足预算会原子纳入路径及两个节点，并准确报告规范 JSON 字节数。

    断言 path_id/kn 引用、节点集合和未截断状态，证明路径没有只保留边而缺少正文；独立字节重算
    同时保证 `used_bytes <= max_bytes` 不是依靠低估中文字符得到的假通过。
    """

    budget = EvidenceBundleBudget(max_bytes=5000, max_nodes=2, max_paths=1)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert {node.node_id for node in bundle.selected_nodes} == {
        "symptom_demo_backlog",
        "root_cause_demo_conflict",
    }
    assert [path.path_id for path in bundle.selected_paths] == ["path_0123456789abcdef"]
    assert all(node.evidence_id.startswith("kn_") for node in bundle.selected_nodes)
    assert bundle.used_bytes == _selected_payload_size(bundle)
    assert bundle.used_bytes <= budget.max_bytes
    assert bundle.truncated is False


def test_zero_path_budget_keeps_seed_but_omits_entire_path_and_unselected_node() -> None:
    """验证路径上限为零时不会泄漏半条路径，只保留可独立解释的高分种子节点。

    根因节点仅由路径引入，因此必须与 path_id 一起进入 omitted 列表；种子仍可作为知识节点证据，
    展示节点和路径预算是相互独立的，不会因为关闭图扩展而返回空成功。
    """

    budget = EvidenceBundleBudget(max_bytes=5000, max_nodes=2, max_paths=0)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert [node.node_id for node in bundle.selected_nodes] == ["symptom_demo_backlog"]
    assert bundle.selected_paths == []
    assert bundle.omitted_node_ids == ["root_cause_demo_conflict"]
    assert bundle.omitted_path_ids == ["path_0123456789abcdef"]
    assert bundle.truncated is True


def test_tiny_byte_budget_never_exceeds_limit_and_reports_all_omissions() -> None:
    """验证最小允许字节预算装不下候选时返回明确截断，而不是超限或截断文本字段。

    构建器允许只保留规范空载荷包装；所有节点和路径 ID 必须出现在 omitted 列表，正文保持完整地
    被省略而不是按字符切断，便于 Planner 知道需要扩大预算或重新检索。
    """

    budget = EvidenceBundleBudget(max_bytes=256, max_nodes=2, max_paths=1)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert bundle.selected_nodes == []
    assert bundle.selected_paths == []
    assert bundle.used_bytes == _selected_payload_size(bundle)
    assert bundle.used_bytes <= budget.max_bytes
    assert set(bundle.omitted_node_ids) == {
        "symptom_demo_backlog",
        "root_cause_demo_conflict",
    }
    assert bundle.omitted_path_ids == ["path_0123456789abcdef"]
    assert bundle.truncated is True
