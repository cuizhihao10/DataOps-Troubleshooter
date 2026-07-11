"""实现 OpenAI-compatible Auditor Structured Outputs 的异步 Provider 边界。

Provider 使用官方 `chat.completions.parse(response_format=AuditResult)` 提交 strict JSON Schema；
它不注册 tools，因为 Auditor 只有读报告并返回 accept/revise 的权限，不能接管 MCP 或数据库。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
)
from pydantic import SecretStr, ValidationError

from app.agents.auditor import (
    AuditorOutputValidationError,
    AuditorProviderError,
    AuditorRefusalError,
)
from app.agents.chat import ChatMessage, validation_failure_details
from app.domain.models import AuditResult

AUDITOR_PROVIDER_CONTRACT_ID = "openai-compatible-auditor:v1"


@runtime_checkable
class AuditorChatProvider(Protocol):
    """声明 Auditor Agent 所需的最小结构化 Chat Provider 接口。

    实现接收完整消息序列并返回已校验 AuditResult；格式、拒绝和传输错误分别映射为 Auditor
    领域异常。协议不暴露 SDK 对象、token 细节或工具调用能力。
    """

    async def complete(self, messages: tuple[ChatMessage, ...]) -> AuditResult:
        """发送至少 system/user 两条消息并返回唯一结构化审计决策。

        输出必须通过 AuditResult 的 accept/revise 跨字段校验，不能是自由字典或修改后的报告；
        Provider 失败时抛异常，不返回默认 accept，也不吞掉拒绝。
        """

        ...


class OpenAICompatibleAuditorProvider:
    """通过官方异步 SDK 调用可配置端点并解析 AuditResult Structured Output。

    SDK 自动从 Pydantic 生成 strict Schema，避免手写 JSON Schema 漂移；`max_retries=0` 禁止隐藏
    网络重试。Provider 只拥有自己创建的 AsyncOpenAI 客户端，注入客户端由外部测试/容器关闭。
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
        """配置凭据、端点、模型和超时，并可注入真实 SDK 客户端测试。

        SecretStr 仅在创建自有客户端时解包；空 URL/模型和非正超时在网络前失败。注入客户端时
        api_key 仍是强类型设置的一部分，但 Provider 不复制或记录其明文值。
        """

        if not base_url.strip():
            raise ValueError("OpenAI-compatible Auditor base_url must not be empty")
        if not model.strip():
            raise ValueError("OpenAI-compatible Auditor model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("OpenAI-compatible Auditor timeout must be positive")
        self.model = model
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def complete(self, messages: tuple[ChatMessage, ...]) -> AuditResult:
        """提交 Auditor 消息并返回 SDK 已解析的 AuditResult。

        请求不传 tools/tool_choice；ValidationError 与长度截断可进入一次 Schema 修复，refusal、
        content filter 和网络/HTTP 错误不能通过格式修复规避。异常摘要不包含响应体或 API key。
        """

        if len(messages) < 2:
            raise ValueError("Auditor completion requires at least system and user messages")
        try:
            # parse 同时提交 strict Schema 和解析响应，保证请求与领域类型使用同一事实来源。
            completion = await self._client.chat.completions.parse(
                model=self.model,
                messages=[message.model_dump(mode="json") for message in messages],
                response_format=AuditResult,
            )
        except ValidationError as exc:
            raw_output, summary = validation_failure_details(exc)
            raise AuditorOutputValidationError(
                validation_summary=summary,
                raw_output=raw_output,
            ) from exc
        except ContentFilterFinishReasonError as exc:
            raise AuditorRefusalError("provider content filter stopped the response") from exc
        except LengthFinishReasonError as exc:
            raise AuditorOutputValidationError(
                validation_summary="model output ended because the length limit was reached",
                raw_output="",
            ) from exc
        except APITimeoutError as exc:
            raise AuditorProviderError(
                error_code="timeout",
                public_summary="Auditor 模型请求超过配置超时。",
                retryable=True,
            ) from exc
        except APIConnectionError as exc:
            raise AuditorProviderError(
                error_code="connection_error",
                public_summary="无法连接 OpenAI-compatible Auditor 服务。",
                retryable=True,
            ) from exc
        except APIStatusError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            error_code = (
                "rate_limited"
                if exc.status_code == 429
                else "authentication_error"
                if exc.status_code in {401, 403}
                else "service_error"
            )
            raise AuditorProviderError(
                error_code=error_code,
                public_summary=f"OpenAI-compatible Auditor 服务返回 HTTP {exc.status_code}。",
                retryable=retryable,
            ) from exc

        # refusal 是安全决策而非 Schema 错误，必须先于 parsed 检查并直接停止审计。
        if not completion.choices:
            raise AuditorOutputValidationError(
                validation_summary="response contained no choices",
                raw_output="",
            )
        message = completion.choices[0].message
        if message.refusal:
            raise AuditorRefusalError(message.refusal)
        if message.parsed is None:
            raise AuditorOutputValidationError(
                validation_summary="response contained no parsed AuditResult",
                raw_output=message.content or "",
            )
        return message.parsed

    async def aclose(self) -> None:
        """关闭 Provider 自行创建的 AsyncOpenAI/httpx 连接池。

        注入客户端由调用方持有，避免两个运行时重复关闭同一连接池；自有客户端在 FastAPI lifespan
        退出时关闭且不吞异常，使资源泄漏能被测试发现。
        """

        if self._owns_client:
            await self._client.close()
