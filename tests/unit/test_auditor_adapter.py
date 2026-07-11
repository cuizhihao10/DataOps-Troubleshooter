"""验证 Auditor Agent 的合法输出、一次 Schema 修复、二次失败和 refusal 边界。

Provider 替身不解析 Prompt，只记录消息与预设结果；测试精确证明只有输出格式错误可修复，模型
拒绝和其他异常不会被第二次请求规避，原始无效文本也不会进入公开异常字符串。
"""

import pytest

from app.agents.auditor import (
    AuditorOutputValidationError,
    AuditorRefusalError,
    AuditorTurnContext,
)
from app.agents.auditor_adapter import OpenAICompatibleAuditorAgent
from app.agents.chat import ChatMessage
from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    get_capability_registry,
)
from app.domain.models import AgentState, AuditResult, AuditStatus, Component, DiagnosisReport


class SequenceAuditorProvider:
    """依次返回 AuditResult 或抛出预设异常，并记录每次不可变消息元组。

    序列耗尽显式失败，防止多余模型调用被默认 accept 掩盖；替身没有网络、工具或报告修改能力，
    只验证 Agent 的消息与修复控制流。
    """

    def __init__(self, outcomes: list[AuditResult | Exception]) -> None:
        """复制结果序列并初始化空调用记录，避免外部列表被就地消费。

        每个结果已经通过 Pydantic，异常是适配层预期类型；构造不读取模板或执行 I/O。序列由
        每个测试独占，调用超额会在 complete 中显式失败。
        """

        self._outcomes = list(outcomes)
        self.calls: list[tuple[ChatMessage, ...]] = []

    async def complete(self, messages: tuple[ChatMessage, ...]) -> AuditResult:
        """记录消息并消费下一项预设结果，超出调用预算时抛断言错误。

        输入不修改；异常按原类型传播，合法 AuditResult 原样返回，使测试聚焦 Agent 而非 Provider。
        """

        self.calls.append(messages)
        if not self._outcomes:
            raise AssertionError("Auditor provider was called more times than expected")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _context() -> AuditorTurnContext:
    """构造含最小报告、终止原因和真实 capability selection 的 Auditor 上下文。

    状态与能力名称完全一致，避免测试绕过 AuditorTurnContext 门禁；空证据允许本组只验证格式修复。
    """

    selection = get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        )
    )
    state = AgentState(
        run_id="run_auditor_adapter_001",
        session_id="session_auditor_adapter_001",
        user_query="审计合成报告",
        intent=selection.intent.value,
        active_capabilities=[name.value for name in selection.active_capabilities],
        stop_reason="evidence_insufficient",
        draft_report=DiagnosisReport(
            summary="无法确认根因。",
            uncertainties=["证据不足。"],
        ),
    )
    return AuditorTurnContext(state=state, capabilities=selection, revision_number=0)


def _accept() -> AuditResult:
    """构造不带问题和指令的合法 accept 结果。

    辅助函数不声称任何业务事实，只证明结构化状态可由 Agent 原样返回；确定性规则是否允许
    accept 由报告工作流测试单独验证。
    """

    return AuditResult(status=AuditStatus.ACCEPT)


def _invalid(raw_output: str = "not-json") -> AuditorOutputValidationError:
    """构造首次 Auditor Schema 失败，携带受控原输出和字段摘要。

    raw_output 只允许进入第二轮 assistant 消息；异常字符串不包含它，attempts 缺省为一。
    """

    return AuditorOutputValidationError(
        validation_summary="root: Invalid JSON",
        raw_output=raw_output,
    )


@pytest.mark.asyncio
async def test_valid_audit_returns_without_repair() -> None:
    """验证首次 AuditResult 合法时只调用 Provider 一次并保留 system/user 顺序。

    repair_count=1 不会预先消费调用；返回对象身份相同，说明 Agent 没有重写 accept 或增加问题。
    """

    result = _accept()
    provider = SequenceAuditorProvider([result])
    agent = OpenAICompatibleAuditorAgent(provider=provider, repair_count=1)

    actual = await agent.review(_context())

    assert actual is result
    assert len(provider.calls) == 1
    assert [message.role.value for message in provider.calls[0]] == ["system", "user"]


@pytest.mark.asyncio
async def test_invalid_output_gets_one_repair_and_second_failure_stops() -> None:
    """验证首次无效 JSON 只追加一次 repair，第二次无效以 attempts=2 停止。

    第二次消息必须为 system/user/assistant/user；总调用数为二且异常字符串不含第二个原输出，
    证明没有递归第三次生成或日志泄漏。
    """

    provider = SequenceAuditorProvider([_invalid("first-bad"), _invalid("second-bad")])
    agent = OpenAICompatibleAuditorAgent(provider=provider, repair_count=1)

    with pytest.raises(AuditorOutputValidationError) as exc_info:
        await agent.review(_context())

    assert exc_info.value.attempts == 2
    assert exc_info.value.raw_output == "second-bad"
    assert "second-bad" not in str(exc_info.value)
    assert len(provider.calls) == 2
    assert [message.role.value for message in provider.calls[1]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert provider.calls[1][2].content == "first-bad"
    assert "不要增加事实" in provider.calls[1][3].content


@pytest.mark.asyncio
async def test_refusal_is_not_repaired() -> None:
    """验证 Auditor refusal 直接传播且不会发送第二次 Schema 修复请求。

    安全拒绝不是 JSON 格式问题；异常公开字符串不能复制供应商策略详情，calls 必须保持一次。
    """

    refusal = AuditorRefusalError("synthetic policy detail")
    provider = SequenceAuditorProvider([refusal])
    agent = OpenAICompatibleAuditorAgent(provider=provider, repair_count=1)

    with pytest.raises(AuditorRefusalError) as exc_info:
        await agent.review(_context())

    assert exc_info.value.refusal == "synthetic policy detail"
    assert "synthetic policy detail" not in str(exc_info.value)
    assert len(provider.calls) == 1
