"""实现 OpenAI-compatible Planner Structured Outputs 的异步 SDK 边界。

Provider 使用官方 `AsyncOpenAI.chat.completions.parse` 从 PlannerDecision 自动生成 strict JSON
Schema；它不注册 API tools，因为真实 MCP Action 始终由 LangGraph 确定性节点执行。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.agents.planner import (
    PlannerOutputValidationError,
    PlannerProviderError,
    PlannerRefusalError,
)
from app.agents.prompts import PLANNER_PROMPT_ID
from app.domain.planner import PlannerDecision
from app.observability import ModelCallMeasurement, ModelCallRole, ModelCallStatus

PLANNER_PROVIDER_CONTRACT_ID = "openai-compatible-planner:v1"


class ChatRole(StrEnum):
    """限定 Planner Provider 可发送的 system、user 与 assistant 消息角色。

    system/user 来自 v4 Prompt；assistant 只在一次修复时回放上次无效输出。禁止 tool/developer
    角色可避免模型供应商自行接管 MCP 工具协议或插入未版本化的高优先级规则。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    """表示发送给 OpenAI-compatible Chat API 的最小文本消息。

    角色由有限枚举控制，content 限制长度并禁止额外字段；模型不支持图片、音频或工具消息，
    保持 Planner 结构化决策调用轻量且可在测试中精确审查 HTTP 请求。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ChatRole
    content: str = Field(min_length=1, max_length=250_000)


@runtime_checkable
class PlannerChatProvider(Protocol):
    """声明 Planner Agent 所需的最小结构化 Chat Provider 接口。

    实现接收完整消息序列并返回已通过 PlannerDecision Pydantic 校验的对象；格式错误、拒绝和
    传输失败分别抛出领域异常，使 Agent 能只修复格式错误而不吞掉其他失败。
    """

    async def complete(self, messages: tuple[ChatMessage, ...]) -> PlannerDecision:
        """向配置模型发送消息，并返回一个 Schema 合法的 PlannerDecision。

        输入至少应包含 system 与 user 消息；输出不能是松散字典。Provider 不执行 Action，
        不返回 Observation，也不在接口中暴露 SDK 响应对象或供应商凭据。
        """

        ...


class OpenAICompatiblePlannerProvider:
    """通过官方异步 SDK 调用可配置 base_url 的 Chat Completions Structured Outputs。

    SDK 的 Pydantic parse helper 自动生成 strict JSON Schema并解析响应，减少手写 Schema 漂移。
    `max_retries=0` 禁止隐藏网络重试；超时、连接和状态码在此转换为稳定领域错误。
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        model: str,
        timeout_seconds: float,
        client: AsyncOpenAI | None = None,
    ) -> None:
        """配置凭据、兼容端点、模型和单次请求超时，并可注入 SDK 客户端测试。

        SecretStr 只在创建 SDK 时解包，实例不保存明文副本；自行创建的客户端关闭责任归 Provider，
        注入客户端由调用方管理。非法空模型/URL或非正超时在任何网络请求前显式失败。
        """

        if not base_url.strip():
            raise ValueError("OpenAI-compatible base_url must not be empty")
        if not model.strip():
            raise ValueError("OpenAI-compatible model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("OpenAI-compatible timeout must be positive")
        self.model = model
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def complete(self, messages: tuple[ChatMessage, ...]) -> PlannerDecision:
        """提交消息并使用 SDK 原生 Pydantic Structured Outputs 解析 PlannerDecision。

        请求不传 tools/function_call，模型只能返回描述性 Action。refusal 单独映射且不进入格式修复；
        Pydantic ValidationError 保存截断原输出供一次修复。SDK/HTTP 失败不自动重试。
        """

        if len(messages) < 2:
            raise ValueError("Planner completion requires at least system and user messages")
        measurement = ModelCallMeasurement(
            role=ModelCallRole.PLANNER,
            provider_contract_id=PLANNER_PROVIDER_CONTRACT_ID,
            model=self.model,
            prompt_contract_id=PLANNER_PROMPT_ID,
        )
        try:
            # parse 同时提交 Pydantic strict Schema 和解析返回，避免请求/响应维护两份手写结构。
            completion = await self._client.chat.completions.parse(
                model=self.model,
                messages=[message.model_dump(mode="json") for message in messages],
                response_format=PlannerDecision,
            )
        except ValidationError as exc:
            # Schema 错误只记录稳定分类；原始输出仍仅在当前修复调用内存中短暂存在。
            measurement.finish(ModelCallStatus.OUTPUT_INVALID)
            raw_output, summary = validation_failure_details(exc)
            raise PlannerOutputValidationError(
                validation_summary=summary,
                raw_output=raw_output,
            ) from exc
        except ContentFilterFinishReasonError as exc:
            measurement.finish(ModelCallStatus.REFUSED)
            raise PlannerRefusalError("provider content filter stopped the response") from exc
        except LengthFinishReasonError as exc:
            measurement.finish(ModelCallStatus.OUTPUT_INVALID)
            raise PlannerOutputValidationError(
                validation_summary="model output ended because the length limit was reached",
                raw_output="",
            ) from exc
        except APITimeoutError as exc:
            measurement.finish(ModelCallStatus.TIMEOUT)
            raise PlannerProviderError(
                error_code="timeout",
                public_summary="Planner 模型请求超过配置超时。",
                retryable=True,
            ) from exc
        except APIConnectionError as exc:
            measurement.finish(ModelCallStatus.CONNECTION_ERROR)
            raise PlannerProviderError(
                error_code="connection_error",
                public_summary="无法连接 OpenAI-compatible Planner 服务。",
                retryable=True,
            ) from exc
        except APIStatusError as exc:
            measurement.finish(ModelCallStatus.HTTP_ERROR)
            retryable = exc.status_code == 429 or exc.status_code >= 500
            error_code = (
                "rate_limited"
                if exc.status_code == 429
                else "authentication_error"
                if exc.status_code in {401, 403}
                else "service_error"
            )
            raise PlannerProviderError(
                error_code=error_code,
                public_summary=(f"OpenAI-compatible Planner 服务返回 HTTP {exc.status_code}。"),
                retryable=retryable,
            ) from exc

        # refusal 必须先于 parsed 检查；安全拒绝不满足业务 Schema，但也不是可修复格式错误。
        if not completion.choices:
            measurement.finish(ModelCallStatus.OUTPUT_INVALID, usage=completion.usage)
            raise PlannerOutputValidationError(
                validation_summary="response contained no choices",
                raw_output="",
            )
        message = completion.choices[0].message
        if message.refusal:
            measurement.finish(ModelCallStatus.REFUSED, usage=completion.usage)
            raise PlannerRefusalError(message.refusal)
        if message.parsed is None:
            measurement.finish(ModelCallStatus.OUTPUT_INVALID, usage=completion.usage)
            raise PlannerOutputValidationError(
                validation_summary="response contained no parsed PlannerDecision",
                raw_output=message.content or "",
            )
        # usage 只投影为 token 数；消息内容和完整 SDK 响应不会进入 recorder。
        measurement.finish(ModelCallStatus.SUCCEEDED, usage=completion.usage)
        return message.parsed

    async def aclose(self) -> None:
        """关闭由 Provider 自行创建的 AsyncOpenAI/httpx 连接池。

        注入客户端的生命周期归测试或依赖容器管理，因此本方法不关闭它；重复调用官方 close 是
        安全的，但调用方仍应在 FastAPI lifespan 退出时只调用一次以保持资源责任清晰。
        """

        if self._owns_client:
            await self._client.close()


def validation_failure_details(exc: ValidationError) -> tuple[str, str]:
    """从 SDK/Pydantic 校验错误提取截断原输出和不含敏感响应体的错误摘要。

    errors() 中的 input 是 SDK 尝试解析的 assistant content，可用于一次修复；summary 只保留字段
    路径和消息，不包含文档 URL或完整 Python repr。多个错误最多保留前十项以控制上下文。
    """

    errors = exc.errors(include_url=False)
    raw_output = ""
    summaries: list[str] = []
    for error in errors[:10]:
        if not raw_output and isinstance(error.get("input"), str):
            raw_output = error["input"]
        location = ".".join(str(part) for part in error.get("loc", ())) or "root"
        summaries.append(f"{location}: {error.get('msg', 'validation failed')}")
    return raw_output[:8000], "; ".join(summaries)[:2000]
