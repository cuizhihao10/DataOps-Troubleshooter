"""定义长期记忆 vector-only 与 vector+SIMILAR_TO 的可复现召回评测。

评测使用版本化合成 corpus/case JSON，调用真实 ``PostgresMemoryRuntime.search`` 的显式模式并计算
Recall@K、Precision@K、graph-only 救回、禁止案例命中和排序回归。模块不调用 LLM，也不把检索层
指标冒充最终诊断准确率；所有汇总结果固定标记为 measured，便于作品集诚实展示实测边界。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import Component, MemoryStatus
from app.memory.models import (
    CaseMemoryMatch,
    MemoryRetrievalChannel,
    MemoryRetrievalMode,
)

MEMORY_RECALL_EVAL_CONTRACT_ID = "memory-recall-eval:v1"


class MemoryEvalCorpusItem(BaseModel):
    """描述评测语料中的一个合成案例及其确定性向量键和目标状态。

    ``label`` 是评测稳定标识，``root_cause`` 用于把实际 CaseMemory 映射回标注，``embedding_key``
    由测试 Provider 选择固定单位向量。状态只允许 confirmed/rejected：前者可召回，后者专门验证
    取消确认隔离；pending 写入行为已有独立测试，不在本小型 corpus 重复建模。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(pattern=r"^memory_[a-z0-9][a-z0-9_-]{1,79}$")
    root_cause: str = Field(min_length=1, max_length=1000)
    component: Component
    embedding_key: str = Field(pattern=r"^angle_(0|30|60|100|120|180|315)$")
    status: MemoryStatus

    @model_validator(mode="after")
    def validate_terminal_status(self) -> MemoryEvalCorpusItem:
        """拒绝 pending corpus 项，确保评测只比较默认可见和明确撤销两种语义。

        confirmed/rejected 原样返回；pending 会抛出 ValidationError，使坏 fixture 在任何数据库写入前
        失败。这样评测不会把“尚未审核”与“已审核后撤销”混成一个禁止标签。
        """

        if self.status is MemoryStatus.PENDING:
            raise ValueError("memory recall eval corpus status must be confirmed or rejected")
        return self


class MemoryRecallEvalCase(BaseModel):
    """描述一条查询的 top-k 期望、禁止标签和应由图单独救回的标签。

    ``query_embedding_key`` 与 corpus 向量键使用同一确定性空间；expected/forbidden 都引用 suite
    label，不使用自由文本模糊匹配。``expected_graph_only_labels`` 必须是 expected 子集，并用于证明
    图关系真实改变候选，而不是只给直接命中附加 graph 元数据。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^memory_recall_[a-z0-9][a-z0-9_-]{2,79}$")
    query: str = Field(min_length=1, max_length=2000)
    query_embedding_key: str = Field(pattern=r"^angle_(0|30|60|100|120|180|315)$")
    limit: int = Field(ge=1, le=20)
    expected_labels: list[str] = Field(min_length=1)
    forbidden_labels: list[str] = Field(default_factory=list)
    expected_graph_only_labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_label_sets(self) -> MemoryRecallEvalCase:
        """校验三个标签集合内部唯一、正负样本不重叠且 graph-only 属于期望集合。

        单案例无法确认 label 是否存在于 corpus，该跨对象检查由 suite 完成；本层先拒绝重复/矛盾
        标注，避免 Recall/Precision 分母和禁止命中计数产生歧义。
        """

        for values in (
            self.expected_labels,
            self.forbidden_labels,
            self.expected_graph_only_labels,
        ):
            if len(values) != len(set(values)):
                raise ValueError("memory recall eval labels must not contain duplicates")
        if set(self.expected_labels) & set(self.forbidden_labels):
            raise ValueError("expected and forbidden memory labels must not overlap")
        if not set(self.expected_graph_only_labels) <= set(self.expected_labels):
            raise ValueError("graph-only memory labels must be expected labels")
        return self


class MemoryRecallEvalSuite(BaseModel):
    """封装版本化合成 corpus 与至少一条召回评测案例并执行跨元素引用校验。

    corpus label/root cause 必须各自唯一，case_id 也不能重复；每个 case 的 expected/forbidden 引用
    必须存在。验证通过后，集成测试才能按同一 suite 建库、生成向量并运行两个检索模式。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["memory-recall-eval:v1"]
    corpus: list[MemoryEvalCorpusItem] = Field(min_length=3)
    cases: list[MemoryRecallEvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_corpus_and_references(self) -> MemoryRecallEvalSuite:
        """检查 corpus/案例唯一性和所有标签引用，返回不可变的可执行 suite。

        校验先比较 label/root/case ID 数量，再检查引用；失败消息因此能区分重复定义和悬空引用。
        任一错误都会阻止评测运行，不能静默跳过坏案例后仍输出看似完整的平均值。
        """

        labels = [item.label for item in self.corpus]
        roots = [item.root_cause for item in self.corpus]
        case_ids = [item.case_id for item in self.cases]
        queries = [item.query for item in self.cases]
        if len(labels) != len(set(labels)):
            raise ValueError("memory recall eval corpus labels must be unique")
        if len(roots) != len(set(roots)):
            raise ValueError("memory recall eval root causes must be unique")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("memory recall eval case IDs must be unique")
        if len(queries) != len(set(queries)):
            raise ValueError("memory recall eval queries must be unique")

        known_labels = set(labels)
        for case in self.cases:
            referenced = set(case.expected_labels) | set(case.forbidden_labels)
            unknown = sorted(referenced - known_labels)
            if unknown:
                raise ValueError(
                    f"memory recall eval case {case.case_id} references unknown labels: {unknown}"
                )
        return self


class MemoryRecallModeMetrics(BaseModel):
    """保存单案例单模式的有序结果、命中集合和检索质量实测值。

    Recall@K 分母是全部 expected 标签，Precision@K 分母是实际返回数；禁止命中与普通 false
    positive 分开列出。``graph_only_hits`` 只统计没有 vector 通道的图候选，直接证明关系扩展贡献。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: MemoryRetrievalMode
    retrieved_labels: list[str]
    expected_hits: list[str]
    missing_expected_labels: list[str]
    false_positive_labels: list[str]
    forbidden_hits: list[str]
    graph_only_hits: list[str]
    recall_at_k: float = Field(ge=0, le=1)
    precision_at_k: float = Field(ge=0, le=1)


class MemoryRecallCaseReport(BaseModel):
    """保存一个案例两种模式指标、差值、图救回标签和预期命中回归。

    正 recall/precision delta 表示在相同 corpus/query/limit 下图模式改善，负值明确暴露回归。
    ``graph_rescued_labels`` 只包含 vector-only 未命中而 vector-graph 命中的 expected 标签；
    ``regressed_labels`` 表示反向丢失，评测测试必须显式审阅而不能隐藏。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    query: str
    limit: int
    vector_only: MemoryRecallModeMetrics
    vector_graph: MemoryRecallModeMetrics
    recall_delta: float = Field(ge=-1, le=1)
    precision_delta: float = Field(ge=-1, le=1)
    graph_rescued_labels: list[str]
    regressed_labels: list[str]


class MemoryRecallEvalReport(BaseModel):
    """汇总整个 suite 的逐案例报告和 macro 平均实测值。

    ``metric_kind`` 固定 measured，防止文档或 UI 把运行结果和产品目标混淆。macro 平均让每个查询
    等权，不因 expected 数量不同而掩盖小案例；禁止命中总数单列为安全回归门禁。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["memory-recall-eval:v1"]
    metric_kind: Literal["measured"] = "measured"
    case_reports: list[MemoryRecallCaseReport] = Field(min_length=1)
    vector_only_macro_recall: float = Field(ge=0, le=1)
    vector_graph_macro_recall: float = Field(ge=0, le=1)
    recall_delta: float = Field(ge=-1, le=1)
    vector_only_macro_precision: float = Field(ge=0, le=1)
    vector_graph_macro_precision: float = Field(ge=0, le=1)
    precision_delta: float = Field(ge=-1, le=1)
    forbidden_hit_count: int = Field(ge=0)


class MemoryRecallSearcher(Protocol):
    """声明评测器所需的最小异步记忆搜索接口。

    生产 ``PostgresMemoryRuntime`` 和单元测试替身均可满足；接口显式要求 mode，使两组实验共享
    query/limit 但只改变图扩展开关。实现错误必须抛出，评测器不能把依赖失败计为零命中。
    """

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        mode: MemoryRetrievalMode = MemoryRetrievalMode.VECTOR_GRAPH,
    ) -> list[CaseMemoryMatch]:
        """按指定模式返回 confirmed raw matches，未命中返回空列表。

        ``query`` 与 ``limit`` 来自已校验 case；返回对象必须遵守 ``case-memory:v2`` 通道/分量契约。
        Provider、数据库或 Schema 失败应传播，不能返回部分列表或自动切换模式。
        """

        ...


def load_memory_recall_eval_suite(path: Path) -> MemoryRecallEvalSuite:
    """从标准 UTF-8 JSON 读取并完整校验版本化长期记忆评测套件。

    文件缺失、JSON 语法、字段 Schema、重复项或悬空引用错误均显式传播；标准 JSON 不写注释，
    字段原理由实现指南和模型 docstring 说明。成功返回不可变 Pydantic suite。
    """

    if not path.is_file():
        raise FileNotFoundError(f"memory recall eval suite does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return MemoryRecallEvalSuite.model_validate(payload)


async def evaluate_memory_recall(
    suite: MemoryRecallEvalSuite,
    searcher: MemoryRecallSearcher,
) -> MemoryRecallEvalReport:
    """对 suite 每个查询顺序运行 vector-only/vector-graph，并计算 macro 实测报告。

    两个模式使用完全相同 query/limit，且顺序执行以避免测试数据库连接池或远端 Provider 并发影响。
    任一搜索失败会终止整个评测，不输出缺案例平均值。结果不包含 embedding、凭据或模型 Thought。
    """

    root_to_label = {item.root_cause: item.label for item in suite.corpus}
    case_reports: list[MemoryRecallCaseReport] = []
    for case in suite.cases:
        # 唯一实验变量是 mode；查询文本、top-k、corpus 和 Provider 均由同一 suite/runtime 固定。
        vector_only_matches = await searcher.search(
            case.query,
            limit=case.limit,
            mode=MemoryRetrievalMode.VECTOR_ONLY,
        )
        vector_graph_matches = await searcher.search(
            case.query,
            limit=case.limit,
            mode=MemoryRetrievalMode.VECTOR_GRAPH,
        )
        vector_only = _mode_metrics(
            case,
            vector_only_matches,
            mode=MemoryRetrievalMode.VECTOR_ONLY,
            root_to_label=root_to_label,
        )
        vector_graph = _mode_metrics(
            case,
            vector_graph_matches,
            mode=MemoryRetrievalMode.VECTOR_GRAPH,
            root_to_label=root_to_label,
        )
        vector_hits = set(vector_only.expected_hits)
        graph_hits = set(vector_graph.expected_hits)
        case_reports.append(
            MemoryRecallCaseReport(
                case_id=case.case_id,
                query=case.query,
                limit=case.limit,
                vector_only=vector_only,
                vector_graph=vector_graph,
                recall_delta=vector_graph.recall_at_k - vector_only.recall_at_k,
                precision_delta=vector_graph.precision_at_k - vector_only.precision_at_k,
                graph_rescued_labels=[
                    label
                    for label in case.expected_labels
                    if label not in vector_hits and label in graph_hits
                ],
                regressed_labels=[
                    label
                    for label in case.expected_labels
                    if label in vector_hits and label not in graph_hits
                ],
            )
        )

    case_count = len(case_reports)
    vector_only_recall = sum(item.vector_only.recall_at_k for item in case_reports) / case_count
    vector_graph_recall = sum(item.vector_graph.recall_at_k for item in case_reports) / case_count
    vector_only_precision = (
        sum(item.vector_only.precision_at_k for item in case_reports) / case_count
    )
    vector_graph_precision = (
        sum(item.vector_graph.precision_at_k for item in case_reports) / case_count
    )
    return MemoryRecallEvalReport(
        contract_id=MEMORY_RECALL_EVAL_CONTRACT_ID,
        case_reports=case_reports,
        vector_only_macro_recall=vector_only_recall,
        vector_graph_macro_recall=vector_graph_recall,
        recall_delta=vector_graph_recall - vector_only_recall,
        vector_only_macro_precision=vector_only_precision,
        vector_graph_macro_precision=vector_graph_precision,
        precision_delta=vector_graph_precision - vector_only_precision,
        forbidden_hit_count=sum(
            len(metrics.forbidden_hits)
            for report in case_reports
            for metrics in (report.vector_only, report.vector_graph)
        ),
    )


def _mode_metrics(
    case: MemoryRecallEvalCase,
    matches: list[CaseMemoryMatch],
    *,
    mode: MemoryRetrievalMode,
    root_to_label: dict[str, str],
) -> MemoryRecallModeMetrics:
    """把一个模式的 raw matches 映射为标签并计算 Recall@K、Precision@K 与安全命中。

    未出现在 corpus 的根因使用 ``unknown:<memory_id>`` 标识并作为 false positive 保留，不能静默
    丢弃。graph-only 命中要求存在 graph 且不存在 vector 通道；vector-only 结果若携带 graph 通道
    直接失败，防止消融实现实际上没有关闭关系扩展。
    """

    if mode is MemoryRetrievalMode.VECTOR_ONLY and any(
        MemoryRetrievalChannel.GRAPH in match.retrieval_channels for match in matches
    ):
        raise ValueError("vector-only memory evaluation cannot contain graph matches")

    # 未知 root cause 不能被过滤，否则 precision 会被人为抬高；保留 memory_id 还能回查污染来源。
    labels = [
        root_to_label.get(match.memory.root_cause, f"unknown:{match.memory.memory_id}")
        for match in matches
    ]
    expected = set(case.expected_labels)
    forbidden = set(case.forbidden_labels)
    expected_hits = [label for label in case.expected_labels if label in labels]
    missing = [label for label in case.expected_labels if label not in labels]
    false_positives = [label for label in labels if label not in expected]
    forbidden_hits = [label for label in labels if label in forbidden]
    graph_only_hits = [
        label
        for label, match in zip(labels, matches, strict=True)
        if label in case.expected_graph_only_labels
        and MemoryRetrievalChannel.GRAPH in match.retrieval_channels
        and MemoryRetrievalChannel.VECTOR not in match.retrieval_channels
    ]
    return MemoryRecallModeMetrics(
        mode=mode,
        retrieved_labels=labels,
        expected_hits=expected_hits,
        missing_expected_labels=missing,
        false_positive_labels=false_positives,
        forbidden_hits=forbidden_hits,
        graph_only_hits=graph_only_hits,
        recall_at_k=len(expected_hits) / len(case.expected_labels),
        precision_at_k=len(expected_hits) / len(labels) if labels else 0.0,
    )
