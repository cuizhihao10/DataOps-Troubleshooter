"""验证长期记忆召回评测 suite、两模式指标、图救回和安全回归门禁。

单元测试使用版本化合成 JSON 与记录型搜索替身，不连接 PostgreSQL；它锁定 Recall@K、Precision@K、
graph-only、forbidden hit、macro 平均和错误模式语义。真实 pgvector/图边由集成评测覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.models import CaseMemory, MemoryStatus
from app.memory.evaluation import (
    MemoryRecallEvalSuite,
    evaluate_memory_recall,
    load_memory_recall_eval_suite,
)
from app.memory.models import (
    CaseMemoryMatch,
    MemoryRetrievalChannel,
    MemoryRetrievalMode,
)

SUITE_PATH = Path("data/evals/memory_recall_cases.json")
NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


class ScriptedMemoryRecallSearcher:
    """按 suite query/mode 返回固定 raw matches，并可故意污染 vector-only 通道。

    正常脚本复现三个案例预期：图救回 case C、直接命中 D、撤销案例查询回退到 C。``leak_graph``
    用于验证消融门禁能发现对照组实际仍在扩图；替身不生成 embedding 或访问数据库。
    """

    def __init__(self, suite: MemoryRecallEvalSuite, *, leak_graph: bool = False) -> None:
        """保存已校验 suite、构造 root 映射并初始化空调用记录。

        ``leak_graph`` 默认为 False；启用时只污染第一条 vector-only 结果。构造不执行搜索或修改
        suite，未知 query/mode 会在 ``search`` 中显式失败。
        """

        self._suite = suite
        self._by_label = {item.label: item for item in suite.corpus}
        self._leak_graph = leak_graph
        self.calls: list[tuple[str, int | None, MemoryRetrievalMode]] = []

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        mode: MemoryRetrievalMode = MemoryRetrievalMode.VECTOR_GRAPH,
    ) -> list[CaseMemoryMatch]:
        """按 case ID 语义返回有序合成匹配，并严格记录 query/limit/mode。

        graph rescue 的 vector-only 返回 A/B，vector-graph 返回 A/C 且 C 为 graph-only；其他案例两
        模式相同。limit 会切片实际结果，未知查询抛 LookupError，避免评测静默得到空结果。
        """

        self.calls.append((query, limit, mode))
        case = next((item for item in self._suite.cases if item.query == query), None)
        if case is None:
            raise LookupError(f"unknown scripted memory recall query: {query}")

        if case.case_id == "memory_recall_graph_rescue":
            if mode is MemoryRetrievalMode.VECTOR_ONLY:
                matches = [
                    self._vector_match("memory_case_a", 0.86),
                    self._vector_match("memory_case_b", 0.70),
                ]
                if self._leak_graph:
                    matches[0] = self._graph_match("memory_case_a", 0.86)
            else:
                matches = [
                    self._vector_match("memory_case_a", 0.86),
                    self._graph_match("memory_case_c", 0.75),
                ]
        elif case.case_id == "memory_recall_direct_baseline":
            matches = [self._vector_match("memory_case_d", 1.0)]
        else:
            matches = [self._vector_match("memory_case_c", 0.77)]
        return matches[:limit]

    def _vector_match(self, label: str, similarity: float) -> CaseMemoryMatch:
        """构造一个只含 vector 通道的 confirmed raw match。

        label 必须存在于 suite corpus；相似度同时作为最终分和 direct 分。任何 fixture 漂移由字典
        KeyError 或 CaseMemoryMatch 校验显式暴露。
        """

        return CaseMemoryMatch(
            memory=self._memory(label),
            similarity=similarity,
            retrieval_channels=[MemoryRetrievalChannel.VECTOR],
            direct_similarity=similarity,
        )

    def _graph_match(self, label: str, score: float) -> CaseMemoryMatch:
        """构造携带稳定 edge 引用的 graph-only confirmed raw match。

        图分同时作为最终分；不提供 direct_similarity，确保评测能把它计入 graph_only_hits。
        """

        return CaseMemoryMatch(
            memory=self._memory(label),
            similarity=score,
            retrieval_channels=[MemoryRetrievalChannel.GRAPH],
            graph_score=score,
            graph_edge_refs=["edge_case_similar_0123456789abcdef"],
        )

    def _memory(self, label: str) -> CaseMemory:
        """把 suite corpus 条目投影为最小 confirmed CaseMemory 供 raw match 使用。

        评测搜索结果只能包含 confirmed，因此即使 corpus 条目是 rejected，本 helper 也只会在脚本错误
        调用时构造并触发语义偏差；正常脚本从不返回 memory_case_e。
        """

        item = self._by_label[label]
        return CaseMemory(
            memory_id=f"mem_{label.removeprefix('memory_case_') * 16}"[:20],
            symptoms=[f"合成症状 {label}"],
            root_cause=item.root_cause,
            components=[item.component],
            evidence_refs=[f"ev_{label}"],
            status=MemoryStatus.CONFIRMED,
            occurrence_count=1,
            created_at=NOW,
            updated_at=NOW,
        )


def test_memory_recall_suite_loads_three_cases_and_validates_cross_references() -> None:
    """确认标准 JSON 加载为 v1 suite，并拒绝 corpus 悬空标签引用。

    正常 fixture 包含五个合成案例和三条查询；随后复制 payload 注入 unknown expected label，必须在
    数据库/Provider 之前由 suite validator 失败。
    """

    suite = load_memory_recall_eval_suite(SUITE_PATH)

    assert suite.contract_id == "memory-recall-eval:v1"
    assert len(suite.corpus) == 5
    assert len(suite.cases) == 3
    assert any(item.status is MemoryStatus.REJECTED for item in suite.corpus)

    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["expected_labels"].append("memory_unknown")
    with pytest.raises(ValidationError, match="unknown labels"):
        MemoryRecallEvalSuite.model_validate(payload)


@pytest.mark.asyncio
async def test_memory_recall_report_measures_graph_gain_and_zero_forbidden_hits() -> None:
    """验证三案例 macro 指标、graph-only 救回和禁止案例安全计数。

    图救回案例让 vector-only 的 recall/precision 为 0.5、图模式为 1；另外两例两模式均为 1，因此
    macro 从 5/6 提升到 1，禁止命中为零。所有数字是固定小样本实测，不代表模型准确率。
    """

    suite = load_memory_recall_eval_suite(SUITE_PATH)
    searcher = ScriptedMemoryRecallSearcher(suite)

    report = await evaluate_memory_recall(suite, searcher)

    assert report.metric_kind == "measured"
    assert report.vector_only_macro_recall == pytest.approx(5 / 6)
    assert report.vector_graph_macro_recall == 1
    assert report.recall_delta == pytest.approx(1 / 6)
    assert report.vector_only_macro_precision == pytest.approx(5 / 6)
    assert report.vector_graph_macro_precision == 1
    assert report.precision_delta == pytest.approx(1 / 6)
    assert report.forbidden_hit_count == 0
    graph_case = report.case_reports[0]
    assert graph_case.graph_rescued_labels == ["memory_case_c"]
    assert graph_case.vector_graph.graph_only_hits == ["memory_case_c"]
    assert graph_case.regressed_labels == []
    assert len(searcher.calls) == 6


@pytest.mark.asyncio
async def test_memory_recall_eval_rejects_graph_leak_in_vector_only_control() -> None:
    """确认对照组若携带 graph 通道，评测立即失败而不是报告虚假增益。

    该门禁防止实现虽然命名为 vector-only 却仍沿 SIMILAR_TO 查询；异常发生在首个案例指标计算，
    不会输出缺失案例的 macro 平均值。
    """

    suite = load_memory_recall_eval_suite(SUITE_PATH)
    searcher = ScriptedMemoryRecallSearcher(suite, leak_graph=True)

    with pytest.raises(ValueError, match="cannot contain graph matches"):
        await evaluate_memory_recall(suite, searcher)
