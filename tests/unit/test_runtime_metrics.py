"""验证 runtime-metrics:v1 的曝光文本顺序稳定、单位显式且非法标签在构造期即被拒绝。

自制 exporter 最常见的两类缺陷是单位歧义和标签语法损坏：后者会让抓取端丢弃整个 job 的全部指标，
因此这里逐字比对完整曝光文本，并验证契约层直接拒绝非法标签与不可能的计数组合，而不是留给渲染。
"""

import pytest
from pydantic import ValidationError

from app.observability import (
    RUNTIME_METRICS_CONTRACT_ID,
    RuntimeMetricsSnapshot,
    SpanMetric,
    render_prometheus_text,
)


def test_snapshot_renders_stable_sorted_exposition_with_explicit_units() -> None:
    """验证快照渲染出的文本按标签排序、单位写在指标名里且以换行结尾。

    逐字比对是有意的：顺序不稳定会让两次抓取的 diff 充满噪声，而缺少末行换行会让部分抓取端丢弃
    最后一个样本——两者都属于"看起来正常但数据错"的故障，只有整体断言才能拦住。
    """

    snapshot = RuntimeMetricsSnapshot(
        run_counts={"succeeded": 3, "failed": 1},
        spans=(
            SpanMetric(
                kind="tool_call",
                name="react.tool_call",
                count=4,
                duration_ms_sum=800.0,
                duration_ms_max=350.5,
                error_count=1,
            ),
            SpanMetric(
                kind="model_call",
                name="auditor.review",
                count=2,
                duration_ms_sum=1200.25,
                duration_ms_max=700.125,
                error_count=0,
            ),
        ),
    )

    assert snapshot.contract_id == RUNTIME_METRICS_CONTRACT_ID
    assert render_prometheus_text(snapshot) == (
        "# HELP dataops_runs_total Persisted diagnosis runs grouped by lifecycle status.\n"
        "# TYPE dataops_runs_total gauge\n"
        'dataops_runs_total{status="failed"} 1\n'
        'dataops_runs_total{status="succeeded"} 3\n'
        "# HELP dataops_span_count Completed trace spans grouped by architecture layer.\n"
        "# TYPE dataops_span_count counter\n"
        'dataops_span_count{kind="model_call",name="auditor.review"} 2\n'
        'dataops_span_count{kind="tool_call",name="react.tool_call"} 4\n'
        "# HELP dataops_span_error_count Trace spans that ended with error status.\n"
        "# TYPE dataops_span_error_count counter\n"
        'dataops_span_error_count{kind="model_call",name="auditor.review"} 0\n'
        'dataops_span_error_count{kind="tool_call",name="react.tool_call"} 1\n'
        "# HELP dataops_span_duration_ms_sum Total span duration in milliseconds.\n"
        "# TYPE dataops_span_duration_ms_sum counter\n"
        'dataops_span_duration_ms_sum{kind="model_call",name="auditor.review"} 1200.250\n'
        'dataops_span_duration_ms_sum{kind="tool_call",name="react.tool_call"} 800.000\n'
        "# HELP dataops_span_duration_ms_max Slowest observed span duration in milliseconds.\n"
        "# TYPE dataops_span_duration_ms_max gauge\n"
        'dataops_span_duration_ms_max{kind="model_call",name="auditor.review"} 700.125\n'
        'dataops_span_duration_ms_max{kind="tool_call",name="react.tool_call"} 350.500\n'
    )


def test_empty_snapshot_still_declares_every_metric_family() -> None:
    """验证空快照仍输出全部五组 HELP/TYPE 声明，让新部署的实例不至于指标"消失"。

    抓取端在指标族缺失与值为 0 之间的表现完全不同：缺失会让看板显示 no data，而使用者往往把
    no data 误读为系统未在运行。声明始终存在则"尚无样本"是可读事实。
    """

    text = render_prometheus_text(RuntimeMetricsSnapshot())
    assert text.count("# TYPE ") == 5
    assert text.endswith("\n")
    assert "dataops_span_count{" not in text


@pytest.mark.parametrize(
    "label",
    ["Tool Call", "react.工具调用", "1react", "a"],
)
def test_unsafe_label_values_are_rejected_at_construction(label: str) -> None:
    """验证含空格、CJK、数字开头或过短的标签值在构造期即失败，不进入渲染。

    渲染函数据此可以无条件拼接字符串。若非法值能到达渲染阶段，曝光文本会语法损坏，抓取端将丢弃
    整个 job 的所有指标，故障范围远大于拒绝这一条记录。
    """

    with pytest.raises(ValidationError):
        SpanMetric(
            kind=label,
            name="react.tool_call",
            count=1,
            duration_ms_sum=1.0,
            duration_ms_max=1.0,
            error_count=0,
        )


def test_error_count_above_total_and_negative_run_count_are_rejected() -> None:
    """验证错误数超过总次数、以及负的 run 计数都被契约拒绝。

    这两个不变量在数据库层没有对应约束（错误数由 FILTER 子句派生），一旦聚合 SQL 写错就会算出
    大于 1 的失败率或负的运行数，看板阈值与告警会同时失去意义。
    """

    with pytest.raises(ValidationError):
        SpanMetric(
            kind="tool_call",
            name="react.tool_call",
            count=1,
            duration_ms_sum=1.0,
            duration_ms_max=1.0,
            error_count=2,
        )
    with pytest.raises(ValidationError):
        RuntimeMetricsSnapshot(run_counts={"failed": -1})
