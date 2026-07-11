"""验证 GraphRAG 消融 Fixture Schema 和实验条件一致性门禁。

快速测试不访问 PostgreSQL，只确保脱敏案例可以加载、预算固定且评测器拒绝错误模式；真实 vector-only
与 vector+graph 指标由 postgres marker 集成测试计算。
"""

from pathlib import Path

import pytest

from app.retrieval.ablation import evaluate_graph_ablation, load_graph_ablation_cases
from app.retrieval.models import (
    GraphRetrievalResult,
    HybridScoringWeights,
    RetrievalMode,
)

ABLATION_CASE_FILE = Path("data/evals/graphrag_ablation_cases.json")


def _empty_result(query: str, mode: RetrievalMode) -> GraphRetrievalResult:
    """构造指定模式的空检索结果，用于隔离评测器的实验条件校验。

    空结果仍携带真实检索契约、Provider 和权重，证明评测函数检查 mode/query 而不依赖数据库内容；
    seeds/paths 采用合法默认空集合。
    """

    return GraphRetrievalResult(
        query=query,
        mode=mode,
        embedding_provider="unit-provider:v1",
        score_weights=HybridScoringWeights(),
    )


def test_ablation_fixture_declares_reproducible_vector_comparison() -> None:
    """验证版本控制中的消融案例固定查询、根因 ID、必要路径和相同检索预算。

    测试不把 JSON 注释扩展加入非标准格式，而是由 Pydantic Schema 和明确断言解释数据；未来新增
    案例若字段缺失、ID 重复或跳数越界会在快速套件中失败。
    """

    cases = load_graph_ablation_cases(ABLATION_CASE_FILE)

    assert len(cases) == 1
    assert cases[0].case_id == "ablation_sync_backlog_causal_chain"
    assert cases[0].expected_root_cause_node_ids == ["root_cause_primary_key_conflict"]
    assert cases[0].required_path_node_ids == [
        "symptom_sync_backlog",
        "root_cause_primary_key_conflict",
        "solution_resolve_pk_conflict",
    ]
    assert cases[0].seed_limit == 5
    assert cases[0].max_hops == 2


def test_ablation_evaluator_rejects_results_from_wrong_modes() -> None:
    """验证评测器不能把 hybrid_graph 或其他实验条件误标成 vector-only 对照组。

    同一查询下故意传入错误模式，期望在计算指标前失败；这防止消融报告因隐藏全文通道或图扩展
    开关不一致而产生看似提升、实际不可比较的结果。
    """

    case = load_graph_ablation_cases(ABLATION_CASE_FILE)[0]
    with pytest.raises(ValueError, match="vector_only result"):
        evaluate_graph_ablation(
            case,
            vector_only=_empty_result(case.query, RetrievalMode.HYBRID_GRAPH),
            vector_graph=_empty_result(case.query, RetrievalMode.VECTOR_GRAPH),
        )
