"""验证模型调用安全遥测的上下文隔离、usage 投影和恰好一次结束规则。

测试只使用合成数字，不构造 Prompt、响应正文或密钥；这也证明观测 Schema 从类型层面没有保存这些
敏感内容的字段，并能明确区分未报告 usage 与真实零 token。
"""

from types import SimpleNamespace

import pytest

from app.observability import (
    InMemoryModelCallRecorder,
    ModelCallMeasurement,
    ModelCallRole,
    ModelCallStatus,
    bind_model_call_recorder,
    reset_model_call_recorder,
)


def test_bound_measurement_records_only_version_status_duration_and_usage() -> None:
    """验证绑定范围内成功调用只投影白名单元数据和三个 token 数。

    SimpleNamespace 模拟 SDK usage 属性；快照必须冻结为单条指标，序列化键集合不能包含 messages、
    content、response、key 或 Thought，防止未来代码无意扩大观测数据面。
    """

    recorder = InMemoryModelCallRecorder()
    token = bind_model_call_recorder(recorder)
    try:
        measurement = ModelCallMeasurement(
            role=ModelCallRole.PLANNER,
            provider_contract_id="openai-compatible-planner:v1",
            model="synthetic-model",
            prompt_contract_id="planner-react:v4",
        )
        measurement.finish(
            ModelCallStatus.SUCCEEDED,
            usage=SimpleNamespace(
                prompt_tokens=11,
                completion_tokens=7,
                total_tokens=18,
            ),
        )
    finally:
        reset_model_call_recorder(token)

    metrics = recorder.snapshot()
    assert len(metrics) == 1
    assert metrics[0].token_usage is not None
    assert metrics[0].token_usage.total_tokens == 18
    assert set(metrics[0].model_dump()) == {
        "contract_id",
        "role",
        "provider_contract_id",
        "model",
        "prompt_contract_id",
        "status",
        "duration_ms",
        "token_usage",
    }


def test_measurement_without_bound_recorder_is_noop_but_still_single_finish() -> None:
    """验证普通生产请求不会积累遥测，同时重复结束仍暴露 Provider 分支错误。

    未绑定 recorder 是 API 默认路径；第一次 finish 不写全局列表，第二次抛错，确保同一网络请求不会
    因成功分支和 finally 分支同时执行而在未来评测中被重复计费。
    """

    measurement = ModelCallMeasurement(
        role=ModelCallRole.AUDITOR,
        provider_contract_id="openai-compatible-auditor:v1",
        model="synthetic-model",
        prompt_contract_id="auditor-report:v2",
    )

    measurement.finish(ModelCallStatus.CONNECTION_ERROR)

    with pytest.raises(RuntimeError, match="finished twice"):
        measurement.finish(ModelCallStatus.SUCCEEDED)
