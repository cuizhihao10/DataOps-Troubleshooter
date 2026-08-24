"""暴露不含 Prompt、原始响应和 Thought 的安全运行观测契约。

观测层只记录模型角色、版本、耗时、状态和 token 计数；具体 Provider 通过上下文绑定的记录器
写入当前评测运行，因此并发任务不会共享隐式的 ``last_usage`` 可变状态。per-run trace 遵循同一
安全边界：span 名与属性只接受 ASCII 标识符字符，自然语言正文在类型层就无法进入遥测。
"""

from app.observability.metrics import (
    RUNTIME_METRICS_CONTRACT_ID,
    RuntimeMetricsSnapshot,
    SpanMetric,
    render_prometheus_text,
)
from app.observability.model_calls import (
    InMemoryModelCallRecorder,
    ModelCallMeasurement,
    ModelCallMetric,
    ModelCallRole,
    ModelCallStatus,
    ModelTokenUsage,
    bind_model_call_recorder,
    reset_model_call_recorder,
)
from app.observability.tracing import (
    RUN_TRACE_CONTRACT_ID,
    RunTrace,
    RunTraceCollector,
    SpanHandle,
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

__all__ = [
    "RUNTIME_METRICS_CONTRACT_ID",
    "RUN_TRACE_CONTRACT_ID",
    "InMemoryModelCallRecorder",
    "ModelCallMeasurement",
    "ModelCallMetric",
    "ModelCallRole",
    "ModelCallStatus",
    "ModelTokenUsage",
    "RunTrace",
    "RunTraceCollector",
    "RuntimeMetricsSnapshot",
    "SpanHandle",
    "SpanMetric",
    "TraceSpan",
    "TraceSpanKind",
    "TraceSpanStatus",
    "bind_model_call_recorder",
    "bind_run_trace_collector",
    "current_run_trace_collector",
    "make_span_id",
    "record_completed_span",
    "render_prometheus_text",
    "reset_model_call_recorder",
    "reset_run_trace_collector",
    "trace_span",
    "traced_node",
]
