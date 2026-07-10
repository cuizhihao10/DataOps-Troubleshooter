"""验证全文/向量种子去重、五项混合评分和图路径评分分量。

这些纯单元测试不启动 PostgreSQL，使用领域模型直接锁定评分公式和排序规则；真实 pgvector 运算、
Provider 空间过滤及递归图扩展由 PostgreSQL 集成测试覆盖。
"""

import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.retrieval.models import (
    GraphPath,
    HybridScoringWeights,
    KnowledgeEdge,
    KnowledgeNode,
    LexicalSeedMatch,
    RetrievalChannel,
    VectorSeedMatch,
)
from app.retrieval.service import merge_seed_matches, score_graph_path


def _node(node_id: str, *, reliability: float = 0.9) -> KnowledgeNode:
    """构造带有效 embedding 溯源的最小知识节点，供评分测试复用。

    固定八维向量满足领域下限，节点 ID 与可靠性由测试覆盖；辅助函数使用生产 Pydantic 模型，
    避免评分测试绕过真实字段约束或依赖数据库 Record。
    """

    return KnowledgeNode(
        node_id=node_id,
        node_type="component",
        name=node_id,
        content=f"synthetic content for {node_id}",
        source_id="synthetic_unit_source",
        source_span=f"source span for {node_id}",
        reliability=reliability,
        embedding=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        embedding_provider="unit-provider:v1",
        embedding_dimensions=8,
    )


def _path(seed: KnowledgeNode) -> GraphPath:
    """构造从给定种子到第二组件的一跳路径，并固定原始边权相关性为 0.8。

    该路径只用于验证混合公式是否把 `GraphPath.score` 作为 path 分量，节点、边和来源仍满足生产
    Schema，使测试同时保护 ScoredGraphPath 的继承字段映射。
    """

    target = _node("component_target", reliability=1.0)
    edge = KnowledgeEdge(
        edge_id="edge_seed_target",
        from_node_id=seed.node_id,
        to_node_id=target.node_id,
        relation_type="DEPENDS_ON",
        weight=0.8,
        source_id="synthetic_unit_source",
        source_span="seed depends on target",
    )
    return GraphPath(
        path_id="path_0123456789abcdef",
        nodes=[seed, target],
        edges=[edge],
        depth=1,
        score=0.8,
        source_ids=["synthetic_unit_source"],
    )


def test_merge_seed_matches_deduplicates_channels_and_preserves_score_components() -> None:
    """验证同一节点的全文与向量候选合并为一个双通道种子，并按公式评分。

    语义 0.8、全文 0.6、可靠性 0.9 在默认权重下应得到 0.51；断言分量和通道而不仅是最终值，
    可防止未来实现改变公式后仍通过仅比较排序的宽松测试。
    """

    node = _node("component_seed")
    weights = HybridScoringWeights()
    merged = merge_seed_matches(
        [LexicalSeedMatch(node=node, lexical_score=0.6)],
        [
            VectorSeedMatch(
                node=node,
                embedding_provider="unit-provider:v1",
                embedding_dimensions=8,
                semantic_score=0.8,
            )
        ],
        weights=weights,
        limit=5,
    )

    assert len(merged) == 1
    assert merged[0].channels == [RetrievalChannel.LEXICAL, RetrievalChannel.VECTOR]
    assert merged[0].semantic_score == 0.8
    assert merged[0].lexical_score == 0.6
    assert merged[0].reliability_score == 0.9
    assert merged[0].hybrid_score == pytest.approx(0.51)


def test_score_graph_path_adds_relation_relevance_without_losing_raw_path_score() -> None:
    """验证路径最终分加入 0.25×边权分，同时保留原始路径分和种子解释分量。

    在前一测试 0.51 的种子分基础上加入 0.8×0.25，应得到 0.71；path.score 仍为 0.8，证明服务
    没有用融合结果覆盖图关系强度，Auditor 可分别解释两者。
    """

    node = _node("component_seed")
    weights = HybridScoringWeights()
    seed = merge_seed_matches(
        [LexicalSeedMatch(node=node, lexical_score=0.6)],
        [
            VectorSeedMatch(
                node=node,
                embedding_provider="unit-provider:v1",
                embedding_dimensions=8,
                semantic_score=0.8,
            )
        ],
        weights=weights,
        limit=5,
    )[0]

    scored = score_graph_path(_path(node), seed=seed, weights=weights)

    assert scored.score == 0.8
    assert scored.hybrid_score == pytest.approx(0.71)
    assert scored.seed_node_id == node.node_id
    assert scored.channels == [RetrievalChannel.LEXICAL, RetrievalChannel.VECTOR]


def test_hybrid_scoring_weights_must_sum_to_one() -> None:
    """验证评分配置不会接受范围内但总和错误的权重组合。

    每项单独合法不足以保证最终分仍处于可比较尺度；构造总和 0.9 的配置应抛出 ValidationError，
    防止部署者遗漏某个分量后服务静默归一化并偏离产品文档。
    """

    with pytest.raises(ValidationError, match="must sum to 1.0"):
        HybridScoringWeights(
            semantic=0.4,
            lexical=0.1,
            path=0.2,
            reliability=0.1,
            freshness=0.1,
        )


def test_settings_reject_invalid_runtime_weight_sum_during_construction() -> None:
    """验证环境配置层在应用启动时就拒绝总和错误的检索权重。

    单独测试 HybridScoringWeights 还不能证明 pydantic-settings 实际调用了该契约；这里覆盖 Settings
    after-validator，确保错误部署不会通过健康检查后等到首次 GraphRAG 请求才失败。
    """

    with pytest.raises(ValidationError, match="must sum to 1.0"):
        Settings(retrieval_semantic_weight=0.40)
