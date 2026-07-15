"""记录真实 Planner/Auditor 模型调用的最小安全遥测。

模块使用 ``ContextVar`` 把记录器绑定到当前异步评测任务，避免在可复用 Provider 上保存并发不安全的
``last_usage``。记录内容严格限于角色、版本、状态、耗时和 token 数，不接收消息、Prompt、模型原始
响应、凭据或 Thought，因此同一实现可以用于本地真实模型评测而不扩大持久化敏感面。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from enum import StrEnum
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_CALL_METRIC_CONTRACT_ID = "model-call-metric:v1"


class ModelCallRole(StrEnum):
    """区分固定双 Agent 中发起结构化模型调用的 Planner 与 Auditor。

    枚举故意不允许任意 Agent 名称，既对应产品固定双 Agent 边界，也阻止评测输出暗示存在第三个
    Agent。工具执行和 embedding 不属于 Chat 调用，因此不进入该枚举。
    """

    PLANNER = "planner"
    AUDITOR = "auditor"


class ModelCallStatus(StrEnum):
    """把一次 SDK 调用归类为成功、结构问题、安全拒绝或稳定传输失败。

    状态只描述公开失败类型，不保存供应商响应正文。``output_invalid`` 可用于统计受控 Schema 修复，
    timeout/connection/http 则保持分离，以便求职演示解释可靠性而不泄露内部异常。
    """

    SUCCEEDED = "succeeded"
    OUTPUT_INVALID = "output_invalid"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    HTTP_ERROR = "http_error"


class ModelTokenUsage(BaseModel):
    """保存供应商公开 usage 中的输入、输出和总 token 计数。

    某些 OpenAI-compatible 服务可能不返回 usage，此时上层让整个对象为 ``None``，不会把未知值伪装
    成零。若返回 usage，三个非负计数必须满足总数等于输入加输出，防止报告聚合漂移。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> ModelTokenUsage:
        """拒绝供应商或测试构造出的不一致 token 总数。

        OpenAI Chat usage 的 total 应等于 prompt 与 completion 之和；若兼容服务未来提供额外 token
        类别，应先升级契约而不是静默吞入当前指标，否则成本比较会失真。
        """

        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("model token total must equal input plus output")
        return self

    @classmethod
    def from_openai_usage(cls, usage: object | None) -> ModelTokenUsage | None:
        """从 SDK usage 对象提取稳定计数，缺失 usage 时明确返回 ``None``。

        通过属性读取而不序列化完整 SDK 响应，确保日志面只接触三个数字。字段缺失或为 ``None`` 说明
        兼容端点没有完整报告，此时保留未知语义，而不是猜测 token 数。
        """

        if usage is None:
            return None
        # 只逐字段读取三个整数，避免 model_dump 完整 SDK usage 时意外扩大供应商元数据面。
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if any(value is None for value in (prompt_tokens, completion_tokens, total_tokens)):
            return None
        return cls(
            input_tokens=int(prompt_tokens),
            output_tokens=int(completion_tokens),
            total_tokens=int(total_tokens),
        )


class ModelCallMetric(BaseModel):
    """表示一次 Provider ``complete`` 的脱敏、可聚合测量结果。

    契约只接受版本标识和数值，不提供任何文本载荷字段，从结构上阻止 Prompt、模型回答或 Thought
    进入评测工件。耗时使用单调时钟计算，避免系统时间校准导致负值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["model-call-metric:v1"] = MODEL_CALL_METRIC_CONTRACT_ID
    role: ModelCallRole
    provider_contract_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    prompt_contract_id: str = Field(min_length=1, max_length=100)
    status: ModelCallStatus
    duration_ms: float = Field(ge=0)
    token_usage: ModelTokenUsage | None = None


class InMemoryModelCallRecorder:
    """按调用完成顺序收集单次真实评测的脱敏模型指标。

    记录器不做文件或数据库 I/O，生命周期由评测 CLI 显式绑定和释放。它适合顺序 Golden 评测；若
    将来并行执行案例，应为每个任务绑定独立实例，再在父任务确定性合并结果。
    """

    def __init__(self) -> None:
        """创建空记录列表，不启动计时器或绑定全局状态。

        构造与 ``ContextVar`` 绑定分离，使调用方可以先准备全部依赖，再只包围需要计量的模型运行；
        这样 FastAPI 启动审计不会被错误计为一次模型调用。
        """

        self._metrics: list[ModelCallMetric] = []

    def record(self, metric: ModelCallMetric) -> None:
        """追加一个已经通过 Pydantic 校验的安全指标。

        方法不接受字典或 SDK 对象，防止调用方绕过字段白名单。列表仅在当前绑定任务内使用，追加
        顺序等于 Provider 完成顺序，可据此识别一次 output_invalid 后的修复调用。
        """

        self._metrics.append(metric)

    def snapshot(self) -> tuple[ModelCallMetric, ...]:
        """返回不可变指标快照，避免报告构造后被后续调用原地修改。

        tuple 只复制引用；各指标本身 frozen，因此调用方既不能追加也不能修改字段。空快照表示绑定
        范围内没有到达模型 Provider，不能被解释为零 token 的成功运行。
        """

        return tuple(self._metrics)


_CURRENT_RECORDER: ContextVar[InMemoryModelCallRecorder | None] = ContextVar(
    "dataops_model_call_recorder",
    default=None,
)


def bind_model_call_recorder(
    recorder: InMemoryModelCallRecorder,
) -> Token[InMemoryModelCallRecorder | None]:
    """把记录器绑定到当前异步上下文并返回必须用于恢复的 token。

    ``ContextVar`` 会随 asyncio task 传播且不会污染其他请求；调用方必须在 ``finally`` 中使用返回
    token 恢复旧值，确保长期运行的 API 进程不会把普通用户请求计入一次已结束的评测。
    """

    return _CURRENT_RECORDER.set(recorder)


def reset_model_call_recorder(token: Token[InMemoryModelCallRecorder | None]) -> None:
    """使用绑定时返回的 token 恢复上一个记录器上下文。

    token 与 ContextVar 由 Python 共同校验，错误任务或重复恢复会显式失败；函数不吞异常，因为遥测
    作用域泄漏会让后续成本数据失真，应在测试或 CLI 中立即可见。
    """

    _CURRENT_RECORDER.reset(token)


class ModelCallMeasurement:
    """封装一次 Provider 调用的单调计时和恰好一次安全落盘动作。

    实例在网络请求前捕获当前 recorder；即使调用结束前上下文发生切换，也只写入起始运行。调用方
    必须在每个成功或异常分支调用 ``finish``，重复调用会失败以防同一请求被双重计费。
    """

    def __init__(
        self,
        *,
        role: ModelCallRole,
        provider_contract_id: str,
        model: str,
        prompt_contract_id: str,
    ) -> None:
        """保存不敏感版本元数据并从单调时钟开始计时。

        构造器不接收消息内容、API key 或 base URL，因此后续无论成功失败都没有泄露这些值的路径。
        当前 recorder 在此捕获，未绑定时 ``finish`` 只结束计时而不产生生产期内存增长。
        """

        self._role = role
        self._provider_contract_id = provider_contract_id
        self._model = model
        self._prompt_contract_id = prompt_contract_id
        self._started = perf_counter()
        self._recorder = _CURRENT_RECORDER.get()
        self._finished = False

    def finish(
        self,
        status: ModelCallStatus,
        *,
        usage: object | None = None,
    ) -> None:
        """结束计时并在存在绑定记录器时追加一条脱敏指标。

        ``usage`` 只会被投影为三个整数；失败分支通常不提供 usage。重复结束表明 Provider 分支设计
        错误并抛 ``RuntimeError``。没有记录器时仍标记完成，以同样的恰好一次规则覆盖生产请求。
        """

        if self._finished:
            raise RuntimeError("model call measurement cannot be finished twice")
        self._finished = True
        # 未绑定是普通 API 的预期路径；直接返回可避免在长期进程中建立隐式全局调用历史。
        if self._recorder is None:
            return
        self._recorder.record(
            ModelCallMetric(
                role=self._role,
                provider_contract_id=self._provider_contract_id,
                model=self._model,
                prompt_contract_id=self._prompt_contract_id,
                status=status,
                duration_ms=(perf_counter() - self._started) * 1000,
                token_usage=ModelTokenUsage.from_openai_usage(usage),
            )
        )
