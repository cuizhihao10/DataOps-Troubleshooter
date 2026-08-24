"""验证 run-trace:v1 的确定性 ID、结构契约、并发父子关系与安全属性边界。

trace 是事后解释"30 秒花在哪"的唯一依据，因此这里既验证正常路径，也验证三类容易被忽略的退化：
序号空洞压实、超上限截断计数、以及自然语言属性被结构性拒绝。所有断言只使用合成 run_id 与数字，
不构造任何 Prompt、Thought 或凭据，从而同时证明遥测层没有承载敏感正文的字段。
"""

import asyncio

import pytest
from pydantic import ValidationError

from app.observability import (
    RUN_TRACE_CONTRACT_ID,
    RunTrace,
    RunTraceCollector,
    TraceSpan,
    TraceSpanKind,
    TraceSpanStatus,
    bind_run_trace_collector,
    current_run_trace_collector,
    make_span_id,
    record_completed_span,
    reset_run_trace_collector,
    trace_span,
    traced_node,
)
from app.orchestration.diagnosis_runtime import _span_stop_reason
from app.orchestration.models import ReactStopReason


def _collect(collector: RunTraceCollector) -> None:
    """在绑定采集器的上下文中打开一个根 span 与一个子 span，供多个用例复用。

    多个用例都需要"根 + 子"这一最小拓扑；抽成函数可以保证它们使用完全相同的绑定与恢复顺序，
    否则某个用例漏掉 reset 会让 ContextVar 泄漏到后续用例，表现为难以定位的顺序相关失败。
    """

    token = bind_run_trace_collector(collector)
    try:
        with trace_span(TraceSpanKind.WORKFLOW, "diagnosis.workflow"):
            with trace_span(TraceSpanKind.NODE, "diagnosis.run_react") as span:
                span.annotate(react_step=2)
    finally:
        reset_run_trace_collector(token)


def test_span_id_is_deterministic_and_rejects_zero_sequence() -> None:
    """验证 span_id 只由 run_id 与序号派生，且序号必须从 1 开始。

    确定性是 Golden 回放可以逐字比对的前提：同一次 run 重新导出必须得到同一组引用。序号从 0 开始
    会与"根 span 序号为 1"的父指针推导冲突，因此必须在派生函数处直接拒绝而不是留给上层校验。
    """

    assert make_span_id("run_demo", 1) == make_span_id("run_demo", 1)
    assert make_span_id("run_demo", 1) != make_span_id("run_demo", 2)
    assert make_span_id("run_demo", 1) != make_span_id("run_other", 1)
    with pytest.raises(ValueError, match="sequence must start at 1"):
        make_span_id("run_demo", 0)


def test_collector_snapshot_builds_single_rooted_parented_trace() -> None:
    """验证嵌套 span 产出唯一根、连续序号且子 span 正确指向父 span。

    父子关系由 ContextVar 推导，一旦推导错误，火焰图只会"看起来有点怪"而不会报错；把唯一根与
    父指针写成断言，可以让插桩传播缺陷在单元测试阶段就失败。
    """

    collector = RunTraceCollector("run_trace_demo")
    _collect(collector)
    trace = collector.snapshot()

    assert trace.contract_id == RUN_TRACE_CONTRACT_ID
    assert [span.sequence for span in trace.spans] == [1, 2]
    assert trace.spans[0].parent_span_id is None
    assert trace.spans[1].parent_span_id == trace.spans[0].span_id
    assert trace.spans[1].attributes == {"react_step": 2}
    assert trace.dropped_span_count == 0
    assert trace.spans_by_kind(TraceSpanKind.NODE) == (trace.spans[1],)


def test_gathered_children_share_one_parent_span() -> None:
    """验证 asyncio.gather 派生的并发 span 全部挂在同一父 span 下，而不是互相覆盖。

    这是 Slice E 并行工具调用的前置条件：若父指针放在共享栈里，两个并发子任务的 push/pop 会交错，
    第二个子 span 会错误地挂到第一个兄弟节点下面，从而伪造出一条不存在的串行依赖链。
    """

    collector = RunTraceCollector("run_parallel_demo")

    async def child(name: str) -> None:
        """在并发任务中打开一个 tool_call span，用于观察父指针是否被兄弟任务污染。

        任务体刻意只做一次 sleep：让两个 span 的生命周期真实重叠，否则顺序执行也能通过断言，
        测试就无法证明 ContextVar 复制语义确实生效。
        """

        with trace_span(TraceSpanKind.TOOL_CALL, name):
            await asyncio.sleep(0)

    async def scenario() -> None:
        """在同一个父 span 内并发执行两个子任务，构造真实的 gather 上下文复制场景。

        绑定与恢复放在协程内部，保证 ContextVar 的作用域与事件循环生命周期一致，避免采集器
        泄漏到同一测试进程内的其他用例。
        """

        token = bind_run_trace_collector(collector)
        try:
            with trace_span(TraceSpanKind.WORKFLOW, "react.loop"):
                await asyncio.gather(child("react.tool_call"), child("react.tool_call"))
        finally:
            reset_run_trace_collector(token)

    asyncio.run(scenario())
    trace = collector.snapshot()

    assert len(trace.spans) == 3
    root = trace.spans[0]
    assert {span.parent_span_id for span in trace.spans[1:]} == {root.span_id}


def test_snapshot_compacts_gaps_left_by_unfinished_spans() -> None:
    """验证未完成序号造成的空洞在导出阶段被压实，并同步改写父指针。

    序号在 span 开始时分配，被强制中断的 span 会留下空洞；空洞违反"序号从 1 连续"的契约，会让
    整个 trace 接口对该 run 报错。压实保留开始顺序并重派生 ID，因此残缺 trace 仍然可读。
    """

    collector = RunTraceCollector("run_gap_demo")
    with collector.open_span(TraceSpanKind.WORKFLOW, "diagnosis.workflow"):
        # 手工预留一个序号且永不完成，等价于 span 在强制中断下未走完结束逻辑。
        collector._reserve_sequence()
        with collector.open_span(
            TraceSpanKind.NODE,
            "diagnosis.run_report",
            parent_span_id=make_span_id("run_gap_demo", 1),
        ):
            pass

    trace = collector.snapshot()
    assert [span.sequence for span in trace.spans] == [1, 2]
    assert [span.name for span in trace.spans] == ["diagnosis.workflow", "diagnosis.run_report"]
    assert trace.spans[1].parent_span_id == trace.spans[0].span_id


def test_span_limit_truncates_and_publishes_dropped_count() -> None:
    """验证超过上限后 span 被丢弃、计数公开，且插桩代码不会因此抛错。

    截断是有意的保护动作，但必须可见：使用者看到非零 dropped_span_count 才知道火焰图不完整，
    而不是误判系统真的只执行了这几步。惰性句柄保证 annotate 在被丢弃的 span 上仍是 no-op。
    """

    collector = RunTraceCollector("run_limit_demo", max_spans=2)
    token = bind_run_trace_collector(collector)
    try:
        for _ in range(4):
            with trace_span(TraceSpanKind.PERSISTENCE, "db.write") as span:
                span.annotate(rows=1)
    finally:
        reset_run_trace_collector(token)

    trace = collector.snapshot()
    assert len(trace.spans) == 2
    assert trace.dropped_span_count == 2


def test_error_and_cancellation_are_classified_separately_and_reraised() -> None:
    """验证异常记为 error、取消记为 cancelled，且两者都原样重抛不改变控制流。

    取消对应 run cancel 与超时这类外部中断，若与缺陷混为一谈，错误率指标会随用户取消行为波动；
    重抛则保证遥测层缺陷不会伪装成业务成功。
    """

    collector = RunTraceCollector("run_status_demo")
    token = bind_run_trace_collector(collector)
    try:
        with trace_span(TraceSpanKind.WORKFLOW, "diagnosis.workflow"):
            with pytest.raises(RuntimeError):
                with trace_span(TraceSpanKind.NODE, "diagnosis.run_react"):
                    raise RuntimeError("synthetic failure")
            with pytest.raises(asyncio.CancelledError):
                with trace_span(TraceSpanKind.NODE, "diagnosis.run_report"):
                    raise asyncio.CancelledError
    finally:
        reset_run_trace_collector(token)

    statuses = {span.name: span.status for span in collector.snapshot().spans}
    assert statuses["diagnosis.run_react"] is TraceSpanStatus.ERROR
    assert statuses["diagnosis.run_report"] is TraceSpanStatus.CANCELLED
    assert statuses["diagnosis.workflow"] is TraceSpanStatus.OK


def test_mark_records_non_exceptional_degradation() -> None:
    """验证 mark 可以把"没抛异常但业务已降级"的 span 记成 error。

    Auditor 否决、重排降级与工具返回 ok=false 都不抛异常，却是必须在 trace 上看见的失败形态；
    若只依赖异常判定状态，这些路径会全部记成 ok，错误率指标随之失去意义。
    """

    collector = RunTraceCollector("run_mark_demo")
    token = bind_run_trace_collector(collector)
    try:
        with trace_span(TraceSpanKind.TOOL_CALL, "react.tool_call") as span:
            span.mark(TraceSpanStatus.ERROR)
    finally:
        reset_run_trace_collector(token)

    assert collector.snapshot().spans[0].status is TraceSpanStatus.ERROR


@pytest.mark.parametrize(
    "attributes",
    [
        {"tool_name": "查询 LTS 状态"},
        {"detail": "planner decided to call the tool"},
        {"工具名": "lts_status"},
        {"note": "x" * 200},
    ],
)
def test_attributes_reject_natural_language_and_oversized_values(
    attributes: dict[str, object],
) -> None:
    """验证含 CJK、空格或超长的属性在写入时即被拒绝，敏感正文无法进入遥测。

    这条约束是"绝不外泄推理过程"的结构性保证：Prompt、Thought 与日志原文必然包含空格或 CJK，
    因此拒绝规则本身就阻断了泄露路径，而不是依赖每个插桩点自觉。
    """

    collector = RunTraceCollector("run_attr_demo")
    token = bind_run_trace_collector(collector)
    try:
        with pytest.raises((ValueError, TypeError)):
            with trace_span(TraceSpanKind.NODE, "diagnosis.run_react", **attributes):
                pass
    finally:
        reset_run_trace_collector(token)


def test_span_name_and_structure_contracts_are_enforced() -> None:
    """验证 span 名称必须是小写点分标识符，且 trace 拒绝悬空父指针与多根。

    名称是聚合维度：一旦包含 run_id 或自然语言，指标基数会爆炸且跨 run 无法对齐。悬空父指针会让
    前端只能画出残树，使用者无法分辨"系统没做这一步"与"这一步的 span 丢了"。
    """

    with pytest.raises(ValidationError):
        TraceSpan(
            run_id="run_demo",
            sequence=1,
            span_id=make_span_id("run_demo", 1),
            kind=TraceSpanKind.NODE,
            name="Diagnosis Run React",
            status=TraceSpanStatus.OK,
            started_at=_now(),
            ended_at=_now(),
            duration_ms=1.0,
        )

    collector = RunTraceCollector("run_multi_root")
    with collector.open_span(TraceSpanKind.WORKFLOW, "diagnosis.workflow"):
        pass
    orphan = collector.snapshot().spans[0]
    with pytest.raises(ValidationError):
        RunTrace(run_id="run_multi_root", spans=(orphan, orphan))


def test_traced_node_and_record_completed_span_join_the_current_trace() -> None:
    """验证装饰器节点与外部计时器桥接都能挂到当前父 span 下。

    模型 Provider 的计时器跨越多个方法，无法改写成 with 块；桥接进来的 span 仍必须挂在正确的
    Agent 节点下，否则 trace 里会出现一层与调用关系不符的"平铺模型调用"。
    """

    collector = RunTraceCollector("run_bridge_demo")

    @traced_node("report.draft")
    async def draft(value: int) -> int:
        """在被装饰节点内桥接一个模型调用 span，模拟真实的 draft_report 节点。

        节点体返回原值以证明装饰器透传返回值：若遥测层吞掉或改写返回值，LangGraph 的状态归并
        会静默丢失该节点的产出。
        """

        record_completed_span(
            TraceSpanKind.MODEL_CALL,
            "model.chat_completion",
            duration_ms=12.5,
            role="planner",
        )
        return value

    async def scenario() -> int:
        """绑定采集器并执行被装饰节点，返回节点结果以便断言返回值未被遥测层改写。

        采集器在协程内部绑定，确保 ContextVar 与事件循环同生命周期，避免泄漏到同进程的其他用例。
        """

        token = bind_run_trace_collector(collector)
        try:
            assert current_run_trace_collector() is collector
            return await draft(7)
        finally:
            reset_run_trace_collector(token)

    assert asyncio.run(scenario()) == 7
    trace = collector.snapshot()
    assert [span.name for span in trace.spans] == ["report.draft", "model.chat_completion"]
    assert trace.spans[0].kind is TraceSpanKind.NODE
    assert trace.spans[1].parent_span_id == trace.spans[0].span_id
    assert trace.spans[1].duration_ms == 12.5


def test_unbound_context_makes_instrumentation_a_noop() -> None:
    """验证未绑定采集器时插桩零成本且不抛错，离线评测与单测无需准备遥测环境。

    这条性质决定了检索、MCP 与 Agent 代码可以无条件插桩：如果 no-op 路径会抛错，插桩点就必须
    到处写分支判断，最终一定有人漏写。
    """

    assert current_run_trace_collector() is None
    with trace_span(TraceSpanKind.RETRIEVAL, "retrieval.graph_channel") as span:
        span.annotate(candidate_count=3)
        assert span.is_recording is False
    record_completed_span(TraceSpanKind.MODEL_CALL, "model.chat_completion", duration_ms=1.0)


def test_stop_reason_is_classified_before_annotation_instead_of_relaxing_the_guard() -> None:
    """验证 ReAct 终止原因先被压成稳定 ASCII 分类再写入 span，模型自述理由不会打断成功的诊断。

    `state.stop_reason` 有两个来源：控制器门禁产生的枚举值（ASCII 标识符），以及 Planner 在
    finish/need_user_input 时自述的自由文本（实测多为中文整句）。首次真实模型冒烟评测里后者直接
    传给 `annotate`，触发属性白名单的 ValueError，被外层 except 记成 `diagnosis_execution_failed`
    ——遥测把一次成功的诊断判成失败。这里断言三条分支都返回可写入的值，从而锁定"降低分辨率而不是
    放宽白名单"这个修复方向；`trace_span` 的实际写入用来证明分类结果确实通得过守卫。
    """

    assert _span_stop_reason(None) == "unspecified"
    assert (
        _span_stop_reason(ReactStopReason.PARALLEL_BUDGET_EXCEEDED.value)
        == ReactStopReason.PARALLEL_BUDGET_EXCEEDED.value
    )
    assert _span_stop_reason("证据已经足够支撑根因结论。") == "planner_reported"

    collector = RunTraceCollector("run_stop_reason_demo")
    token = bind_run_trace_collector(collector)
    try:
        with trace_span(TraceSpanKind.WORKFLOW, "diagnosis.workflow") as span:
            span.annotate(stop_reason=_span_stop_reason("证据已经足够支撑根因结论。"))
    finally:
        reset_run_trace_collector(token)

    assert collector.snapshot().spans[0].attributes["stop_reason"] == "planner_reported"


def _now() -> object:
    """返回一个带时区的当前时间，供构造非法 span 的用例填充必填时间戳字段。

    单独抽出是为了让这些用例聚焦被验证的字段（名称、父指针），时间戳只是让模型能进入校验阶段的
    必要输入，不参与断言。
    """

    from datetime import UTC, datetime

    return datetime.now(UTC)
