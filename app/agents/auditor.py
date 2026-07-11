"""定义独立 Auditor Agent 与报告审计工作流之间的强类型协议和安全失败。

Auditor 只能读取草稿、证据、能力规则和确定性预检结果，返回 AuditResult；它不执行工具、不写
长期记忆，也不直接修改报告。异常只公开稳定停止原因，不暴露模型响应体或凭据。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import CapabilitySelection
from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditResult,
    CaseMemory,
    MemoryStatus,
)
from app.retrieval.models import GraphEvidenceBundle


class AuditorAgentError(RuntimeError):
    """表示可由报告工作流安全降级的预期 Auditor 失败。

    异常字符串只包含 public_summary；原始输出、refusal 和 SDK 异常保存在受控属性或异常链中，
    不进入 AgentState、公开事件或 API。未预期编程错误不继承本类，应继续传播。
    """

    def __init__(self, *, stop_reason: str, public_summary: str) -> None:
        """保存稳定停止原因和净化摘要，并初始化标准 RuntimeError 消息。

        两个输入由具体子类固定或截断；调用方无需解析供应商字符串即可路由。空值不被静默填充，
        具体异常构造器负责提供可审计语义。
        """

        super().__init__(public_summary)
        self.stop_reason = stop_reason
        self.public_summary = public_summary


class AuditorOutputValidationError(AuditorAgentError):
    """表示 AuditResult 未通过 JSON/Pydantic 校验，可触发一次 Schema 修复。

    raw_output 仅在当前调用内回放且最多八千字符；RuntimeError 消息不包含它。attempts 为一或二，
    二次失败后报告工作流直接降级，不能把无效 accept/revise 当作控制信号。
    """

    def __init__(
        self,
        *,
        validation_summary: str,
        raw_output: str,
        attempts: int = 1,
    ) -> None:
        """保存截断校验信息和生成次数，并设置 auditor_output_invalid 停止原因。

        attempts 仅允许一或二，防止适配器意外形成无界修复；原输出不会传给父异常，也不会由
        LangGraph 写入公开状态。validation_summary 只用于下一次 Schema 修复。
        """

        if attempts not in {1, 2}:
            raise ValueError("Auditor output attempts must be 1 or 2")
        super().__init__(
            stop_reason="auditor_output_invalid",
            public_summary=f"Auditor 结构化输出在 {attempts} 次生成后仍未通过 Schema 校验。",
        )
        self.validation_summary = validation_summary[:2000]
        self.raw_output = raw_output[:8000]
        self.attempts = attempts


class AuditorRefusalError(AuditorAgentError):
    """表示模型结构化拒绝审计请求，且该失败不能通过 Schema 修复规避。

    refusal 文本可能包含供应商策略信息，只截断保存在属性中；公开摘要固定为审计不可用，报告
    工作流据此生成安全降级结果而不是假装已通过。
    """

    def __init__(self, refusal: str) -> None:
        """保存截断 refusal，并设置稳定 auditor_refusal 停止原因。

        空字符串转换为通用占位，不猜测供应商原因；父异常不接收原始文本，通用异常日志因此不会
        泄露完整拒绝内容。
        """

        super().__init__(
            stop_reason="auditor_refusal",
            public_summary="Auditor 模型拒绝生成本轮结构化审计结果。",
        )
        self.refusal = (refusal or "unspecified refusal")[:2000]


class AuditorProviderError(AuditorAgentError):
    """表示 Auditor 的超时、连接、认证、限流或服务端失败。

    Provider 禁用 SDK 隐式重试，retryable 仅作为诊断属性；本次工作流不会因网络失败放行报告，
    也不会把 URL、响应体或 API key 写入公开摘要。
    """

    def __init__(
        self,
        *,
        error_code: str,
        public_summary: str,
        retryable: bool,
    ) -> None:
        """初始化供应商无关错误分类、净化摘要和未来可重试提示。

        retryable 不触发当前请求的第二次网络尝试，以免与总预算叠加；调用方只用 stop_reason 决定
        安全降级，error_code 用于受控日志或指标。
        """

        super().__init__(stop_reason="auditor_provider_error", public_summary=public_summary)
        self.error_code = error_code
        self.retryable = retryable


class AuditorTurnContext(BaseModel):
    """封装 Auditor 单轮所需的报告、证据、能力规则和确定性问题。

    `state.draft_report` 必须存在；confirmed memories 只能为 confirmed；revision_number 只允许零或
    一。模型冻结并禁止额外字段，避免把 SDK 对象、Thought 或未审计数据塞入 Prompt。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AgentState
    capabilities: CapabilitySelection
    evidence_bundle: GraphEvidenceBundle | None = None
    confirmed_case_memories: tuple[CaseMemory, ...] = ()
    deterministic_issues: tuple[AuditIssue, ...] = ()
    revision_number: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_audit_context(self) -> AuditorTurnContext:
        """保证审计发生在草稿生成后，且历史案例和能力状态没有漂移。

        校验先检查 draft，再检查 intent/能力一致性和案例确认状态；任一失败都在调用模型前暴露，
        防止 Auditor 审查半初始化或被 pending memory 污染的上下文。
        """

        if self.state.draft_report is None:
            raise ValueError("Auditor context requires a draft report")
        expected_names = [name.value for name in self.capabilities.active_capabilities]
        if self.state.intent != self.capabilities.intent.value:
            raise ValueError("state intent must match Auditor capability selection")
        if self.state.active_capabilities != expected_names:
            raise ValueError("state capabilities must match Auditor capability selection")
        if any(
            memory.status is not MemoryStatus.CONFIRMED for memory in self.confirmed_case_memories
        ):
            raise ValueError("Auditor context can include only confirmed case memories")
        return self


@runtime_checkable
class AuditorAgent(Protocol):
    """声明独立 Auditor 必须实现的异步结构化审计接口。

    实现可以使用 OpenAI-compatible Provider 或测试替身，但只能返回 AuditResult；不能返回修改后
    报告或执行工具。预期供应商失败应映射为 AuditorAgentError，编程错误继续传播。
    """

    async def review(self, context: AuditorTurnContext) -> AuditResult:
        """审查当前草稿并返回唯一 accept/revise 决策。

        输入包含完整强类型上下文，输出必须通过 AuditResult 跨字段校验；实现不得保存原始推理，
        不得把自由文本解析为工作流控制，也不得忽略 deterministic_issues 的否决语义。
        """

        ...
