"""验证全文/向量种子去重、五项混合评分、cross-encoder 两阶段重排和图路径评分分量。

这些纯单元测试不启动 PostgreSQL，使用领域模型与替身仓储直接锁定评分公式、排序规则以及"重排失败
即降级为一阶段"的语义；真实 pgvector 运算、Provider 空间过滤及递归图扩展由 PostgreSQL 集成测试
覆盖，真实 cross-encoder 的 HTTP 契约由 `tests/unit/test_reranker.py` 覆盖。
"""

from collections.abc import Iterable, Sequence

import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.retrieval.models import (
    GraphPath,
    HybridScoringWeights,
    KnowledgeEdge,
    KnowledgeNode,
    LexicalSeedMatch,
    RetrievalChannel,
    VectorSeedMatch,
)
from app.retrieval.reranker import RerankerError
from app.retrieval.service import GraphRetrievalService, merge_seed_matches, score_graph_path


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


class _StubGraphRepository:
    """替身图仓储，返回预设的两路候选并记录服务实际请求的 top-k。

    两阶段检索的收益完全取决于一阶段是否按倍数多召回，因此"服务向数据库要了多少条"必须可断言，
    而不是只看最终种子数量；替身同时避免这些评分测试依赖 PostgreSQL 与真实 pgvector 运算。
    """

    def __init__(
        self,
        *,
        lexical: list[LexicalSeedMatch] | None = None,
        vector: list[VectorSeedMatch] | None = None,
    ) -> None:
        """保存两路预设候选，并初始化用于断言候选放大倍数的 limit 记录列表。

        默认空列表让测试只声明关心的通道；`requested_limits` 按调用顺序记录 lexical 与 vector 的
        top-k，因此候选上限被仓储契约或重排端点截断时，断言会立刻指出被截断的那一次调用。
        """

        self._lexical = lexical or []
        self._vector = vector or []
        self.requested_limits: list[int] = []

    async def search_lexical_seeds(self, query: str, *, limit: int) -> list[LexicalSeedMatch]:
        """记录本次请求的 top-k 并返回预设全文候选，不做任何真实分词或排序。

        全文分数由测试直接给出，因为这里验证的是融合与重排逻辑；查询串不参与筛选，避免替身悄悄
        实现一套与 PostgreSQL ts_rank 不同的匹配规则而让断言失去意义。
        """

        self.requested_limits.append(limit)
        return list(self._lexical)

    async def search_vector_seeds(
        self,
        embedding: Sequence[float],
        *,
        provider_id: str,
        limit: int,
    ) -> list[VectorSeedMatch]:
        """记录本次请求的 top-k 并返回预设向量候选，忽略查询向量与 Provider 过滤。

        Provider ID 与维度过滤属于 SQL 行为，由 PostgreSQL 集成测试覆盖；此处保留完整签名，使服务
        改变调用方式时测试以 TypeError 失败，而不是静默走进一条未被验证的分支。
        """

        self.requested_limits.append(limit)
        return list(self._vector)

    async def expand_paths(
        self,
        node_id: str,
        *,
        max_hops: int,
        allowed_relations: Iterable[object],
    ) -> list[GraphPath]:
        """返回空路径集合，把断言范围限制在种子召回、重排融合与截断三个步骤上。

        路径继承重排分的规则由 `score_graph_path` 的专项测试直接覆盖；服务级测试不再重复构造图结构，
        以免路径评分的细节变化让本应只关心种子排序的用例一起失败。
        """

        return []


class _StubReranker:
    """替身 cross-encoder，按节点 ID 给出预设分数或抛出领域异常以验证降级。

    真实端点的请求体、乱序回填和契约校验由 `tests/unit/test_reranker.py` 覆盖；这里只需要一个可
    预测的第二阶段分数来源，才能把"名次变化来自精排"这一结论写成精确数值断言而不是模糊比较。
    """

    model = "BAAI/bge-reranker-v2-m3"

    def __init__(self, scores_by_node_id: dict[str, float], *, fail: bool = False) -> None:
        """保存节点 ID 到重排分的映射，并记录是否模拟远程失败。

        用节点 ID 而不是文档下标建立映射，使测试不必假设服务传入候选的顺序；`fail=True` 让替身抛出
        `RerankerError`，用于验证"计费外部服务抖动不会击穿检索链路"这条可用性边界。
        """

        self._scores_by_node_id = scores_by_node_id
        self._fail = fail

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """按输入顺序返回每个候选的预设分数，文档首行即节点 ID。

        `knowledge_node_text` 把名称放在首行，而测试节点的名称等于其 ID，因此首行可以稳定定位映射；
        遇到未预设的候选立即断言失败，避免替身用默认值掩盖服务多送或少送候选的缺陷。
        """

        if self._fail:
            raise RerankerError("stub reranker is unavailable")
        scores: list[float] = []
        for document in documents:
            node_id = document.splitlines()[0]
            assert node_id in self._scores_by_node_id, f"unexpected rerank candidate: {node_id}"
            scores.append(self._scores_by_node_id[node_id])
        return scores


def _vector_match(node_id: str, semantic_score: float) -> VectorSeedMatch:
    """构造一个只命中向量通道的种子候选，Provider 元数据与测试节点保持一致。

    只声明向量通道可以让一阶段分数完全由语义分与可靠性决定，使后续"重排改变名次"的断言不必同时
    推导全文分量；Provider ID 与维度仍走生产模型字段，避免测试绕过向量溯源约束。
    """

    return VectorSeedMatch(
        node=_node(node_id),
        embedding_provider="unit-provider:v1",
        embedding_dimensions=8,
        semantic_score=semantic_score,
    )


def _service(
    repository: _StubGraphRepository,
    *,
    reranker: _StubReranker | None = None,
    rerank_candidate_multiplier: int = 3,
) -> GraphRetrievalService:
    """用替身仓储、离线 Embedding Provider 和可选替身重排器组装检索服务。

    离线 feature-hash Provider 让服务能真实执行"生成一个查询向量并校验维度"这一步而无需凭据；
    融合权重固定为默认 0.4，使断言中的融合结果可以直接用产品文档里的公式手算复核。
    """

    return GraphRetrievalService(
        repository,  # type: ignore[arg-type]
        DeterministicHashEmbeddingProvider(dimensions=8),
        reranker=reranker,  # type: ignore[arg-type]
        rerank_candidate_multiplier=rerank_candidate_multiplier,
        rerank_blend_weight=0.4,
    )


@pytest.mark.asyncio
async def test_rerank_overfetches_candidates_and_can_change_the_surviving_seed() -> None:
    """验证启用重排后按倍数多召回，融合分决定截断结果，并如实记录模型与候选规模。

    一阶段 beta（0.45）领先 alpha（0.27），但 cross-encoder 给 alpha 0.95、给 beta 0.10，按 0.4 融合
    后 alpha 得 0.542、beta 得 0.31。seed_limit=1 时只有真正执行了第二阶段的实现才会留下 alpha，
    因此这条断言同时证明重排既参与排序又参与"哪些证据进入上下文"的取舍。
    """

    repository = _StubGraphRepository(
        vector=[_vector_match("component_alpha", 0.4), _vector_match("component_beta", 0.8)]
    )
    reranker = _StubReranker({"component_alpha": 0.95, "component_beta": 0.10})

    result = await _service(repository, reranker=reranker).retrieve("任务卡住", seed_limit=1)

    assert repository.requested_limits == [3, 3]
    assert result.candidate_count == 2
    assert result.reranker_model == "BAAI/bge-reranker-v2-m3"
    assert result.rerank_blend_weight == 0.4
    assert len(result.seeds) == 1
    assert result.seeds[0].node.node_id == "component_alpha"
    assert result.seeds[0].hybrid_score == pytest.approx(0.27)
    assert result.seeds[0].rerank_score == pytest.approx(0.95)
    assert result.seeds[0].final_score == pytest.approx(0.542)


@pytest.mark.asyncio
async def test_reranker_failure_degrades_to_one_stage_and_says_so_in_the_result() -> None:
    """验证重排端点失败时检索仍返回一阶段排序，且结果如实声明未执行第二阶段。

    重排是可选增强，把它变成可用性依赖会让一个计费外部服务抖动直接击穿整条诊断链路；同时
    `reranker_model` 为空、`rerank_blend_weight` 归零、`final_score` 等于 `hybrid_score` 三者必须
    同时成立，否则报告会把未精排的顺序说成精排结果。
    """

    repository = _StubGraphRepository(
        vector=[_vector_match("component_alpha", 0.4), _vector_match("component_beta", 0.8)]
    )
    reranker = _StubReranker({}, fail=True)

    result = await _service(repository, reranker=reranker).retrieve("任务卡住", seed_limit=1)

    assert result.reranker_model is None
    assert result.rerank_blend_weight == 0
    assert result.candidate_count == 2
    assert result.seeds[0].node.node_id == "component_beta"
    assert result.seeds[0].rerank_score is None
    assert result.seeds[0].final_score == pytest.approx(result.seeds[0].hybrid_score)


@pytest.mark.asyncio
async def test_candidate_overfetch_is_capped_by_the_repository_top_k_contract() -> None:
    """验证放大后的候选条数受上限约束，不会向仓储或重排端点请求超出契约的批量。

    倍数是可配置项，8×5 会得到 40，但仓储 top-k 与重排单次文档数都有上界；若不在本地设限，请求会被
    远程静默截断，评测里的"候选规模"分母就与实际不符，两阶段收益也无法解释。
    """

    repository = _StubGraphRepository()
    service = _service(
        repository,
        reranker=_StubReranker({}),
        rerank_candidate_multiplier=8,
    )

    result = await service.retrieve("任务卡住", seed_limit=5)

    assert repository.requested_limits == [20, 20]
    assert result.candidate_count == 0
    assert result.reranker_model is None


def test_scored_path_inherits_the_seed_rerank_score_with_the_same_blend_weight() -> None:
    """验证路径按同一融合权重继承种子重排分，且一阶段路径分与原始边权分都保持可见。

    路径的相关性来源是"这个种子值得展开"，把拼接文本再送一次 cross-encoder 只增加成本不增加信息；
    继承后三层分数（0.8 边权分、0.71 一阶段分、0.786 融合分）必须同时保留，删边消融和评分调参才能
    判断名次变化来自图结构、种子召回还是第二阶段。
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
    )[0].model_copy(update={"rerank_score": 0.9, "final_score": 0.786})

    scored = score_graph_path(_path(node), seed=seed, weights=weights, rerank_blend_weight=0.4)

    assert scored.score == 0.8
    assert scored.hybrid_score == pytest.approx(0.71)
    assert scored.rerank_score == pytest.approx(0.9)
    assert scored.final_score == pytest.approx(0.786)
