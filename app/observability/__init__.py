"""暴露不含 Prompt、原始响应和 Thought 的安全运行观测契约。

观测层只记录模型角色、版本、耗时、状态和 token 计数；具体 Provider 通过上下文绑定的记录器
写入当前评测运行，因此并发任务不会共享隐式的 ``last_usage`` 可变状态。
"""

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

__all__ = [
    "InMemoryModelCallRecorder",
    "ModelCallMeasurement",
    "ModelCallMetric",
    "ModelCallRole",
    "ModelCallStatus",
    "ModelTokenUsage",
    "bind_model_call_recorder",
    "reset_model_call_recorder",
]
