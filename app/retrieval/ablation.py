"""定义可复现的 vector-only 与 vector+graph 消融案例、指标和评测函数。

消融评测不调用 LLM，也不把检索分数解释为正确答案。它只比较同一查询、Provider、种子上限和
跳数预算下，图扩展是否增加预先标注根因节点的可见性与必要有序链路的完整率；案例来自脱敏 JSON，
结果由结构化模型返回，便于测试和后续评测报告保存实测值。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.retrieval.models import GraphRetrievalResult, RetrievalMode


class GraphAblationCase(BaseModel):
    """描述一条脱敏消融查询及其预期根因节点和必要有序路径。

    `seed_limit` 与 `max_hops` 固定评测预算，避免两个模式使用不同搜索空间；根因和路径只引用人工
    知识图稳定 ID，不使用自由文本近似匹配，从而让结果能够跨代码版本重复计算。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^ablation_[a-z0-9][a-z0-9_-]{2,79}$")
    query: str = Field(min_length=1, max_length=2000)
    expected_root_cause_node_ids: list[str] = Field(min_length=1)
    required_path_node_ids: list[str] = Field(min_length=2)
    seed_limit: int = Field(default=5, ge=1, le=20)
    max_hops: int = Field(default=2, ge=1, le=2)


class AblationModeMetrics(BaseModel):
    """保存单个检索模式的根因命中、链路完整率和实际路径引用。

    根因命中是布尔值，链路完整率是必要有序节点在最佳路径中的覆盖比例；路径 ID 列表允许审阅者
    回到真实边关系核对指标，不会只看到一个无来源分数。
    """

    model_config = ConfigDict(extra="forbid")

    mode: RetrievalMode
    root_cause_hit: bool
    chain_completeness: float = Field(ge=0, le=1)
    matched_path_ids: list[str] = Field(default_factory=list)


class GraphAblationReport(BaseModel):
    """保存同一案例 vector-only 与 vector+graph 指标及其有符号差值。

    报告字段均为实际结构计算结果而非目标值；正差表示图扩展带来增益，零表示持平，负差会让测试
    明确暴露回归。报告不声称 LLM 最终根因命中，因为当前切片尚未接入 Planner。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    query: str
    vector_only: AblationModeMetrics
    vector_graph: AblationModeMetrics
    root_cause_hit_delta: int = Field(ge=-1, le=1)
    chain_completeness_delta: float = Field(ge=-1, le=1)


def load_graph_ablation_cases(path: Path) -> list[GraphAblationCase]:
    """从标准 UTF-8 JSON 加载并校验消融案例，同时拒绝重复 case_id。

    TypeAdapter 校验顶层列表和每个字段，集合检查负责跨元素唯一性；文件、JSON 或 Schema 错误直接
    传播，使评测不能静默跳过坏案例。标准 JSON 不含注释，字段原理记录在实现指南。
    """

    if not path.is_file():
        raise FileNotFoundError(f"GraphRAG ablation case file does not exist: {path}")

    # 单案例字段先由 Pydantic 校验，随后再检查只有整个列表视角才能发现的 case_id 冲突。
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = TypeAdapter(list[GraphAblationCase]).validate_python(payload)
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("GraphRAG ablation cases contain duplicate case IDs")
    return cases


def evaluate_graph_ablation(
    case: GraphAblationCase,
    *,
    vector_only: GraphRetrievalResult,
    vector_graph: GraphRetrievalResult,
) -> GraphAblationReport:
    """验证两份结果模式/查询一致，并计算根因命中与必要链路完整率差值。

    根因节点可以由种子或图路径暴露；链路完整率只读取真实路径中的有序 node_ids，vector-only 因
    没有路径自然为零。输入模式或查询不匹配会立即失败，防止把不同实验条件误报为图增益。
    """

    if vector_only.mode is not RetrievalMode.VECTOR_ONLY:
        raise ValueError("vector_only result must use vector_only mode")
    if vector_graph.mode is not RetrievalMode.VECTOR_GRAPH:
        raise ValueError("vector_graph result must use vector_graph mode")
    if vector_only.query != case.query or vector_graph.query != case.query:
        raise ValueError("ablation results must use the case query")
    if vector_only.embedding_provider != vector_graph.embedding_provider:
        raise ValueError("ablation results must use the same embedding provider")
    if vector_only.score_weights != vector_graph.score_weights:
        raise ValueError("ablation results must use the same scoring weights")
    for result in (vector_only, vector_graph):
        if result.seed_limit != case.seed_limit or result.max_hops != case.max_hops:
            raise ValueError("ablation results must use the case retrieval budgets")

    # 只有所有实验条件一致后才计算差值，避免“换 Provider/预算”被错误归因于图结构。
    vector_only_metrics = _mode_metrics(case, vector_only)
    vector_graph_metrics = _mode_metrics(case, vector_graph)
    return GraphAblationReport(
        case_id=case.case_id,
        query=case.query,
        vector_only=vector_only_metrics,
        vector_graph=vector_graph_metrics,
        root_cause_hit_delta=(
            int(vector_graph_metrics.root_cause_hit) - int(vector_only_metrics.root_cause_hit)
        ),
        chain_completeness_delta=(
            vector_graph_metrics.chain_completeness - vector_only_metrics.chain_completeness
        ),
    )


def _mode_metrics(
    case: GraphAblationCase,
    result: GraphRetrievalResult,
) -> AblationModeMetrics:
    """从种子和路径收集可见根因，并选择必要有序链覆盖率最高的路径。

    根因 ID 集合同时包含种子节点和路径节点，体现图扩展可增加候选可见性；链路匹配保持顺序但允许
    路径包含额外节点，返回所有达到最佳覆盖率的 path_id 便于人工核验。
    """

    visible_node_ids = {seed.node.node_id for seed in result.seeds}
    visible_node_ids.update(node.node_id for path in result.paths for node in path.nodes)
    expected_root_causes = set(case.expected_root_cause_node_ids)
    root_cause_hit = bool(visible_node_ids & expected_root_causes)

    path_coverages = [
        (
            path.path_id,
            _ordered_path_coverage(
                [node.node_id for node in path.nodes],
                case.required_path_node_ids,
            ),
        )
        for path in result.paths
    ]
    best_coverage = max((coverage for _, coverage in path_coverages), default=0.0)
    matched_path_ids = sorted(
        path_id
        for path_id, coverage in path_coverages
        if coverage == best_coverage and coverage > 0
    )
    return AblationModeMetrics(
        mode=result.mode,
        root_cause_hit=root_cause_hit,
        chain_completeness=best_coverage,
        matched_path_ids=matched_path_ids,
    )


def _ordered_path_coverage(actual: list[str], required: list[str]) -> float:
    """计算实际路径对必要节点序列的最长有序子序列覆盖比例。

    使用双指针只按顺序消费 required 节点，允许实际路径包含额外中间实体但不允许倒序命中；比例
    分母固定为人工标注必要节点数。该指标适合一至两跳小图，不引入复杂图编辑距离。
    """

    matched = 0
    for node_id in actual:
        if matched < len(required) and node_id == required[matched]:
            matched += 1
    return matched / len(required)
