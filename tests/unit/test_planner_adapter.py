"""验证 Planner Agent 的合法输出、一次修复、二次失败和 refusal 边界。

测试使用协议替身隔离 HTTP SDK，精确检查消息序列和调用次数。只有 Schema 校验失败允许修复；
拒绝或 Provider 错误必须直接传播，防止适配器通过重试规避安全决策或放大故障。
"""

import pytest

from app.agents.chat import ChatMessage
from app.agents.planner import (
    PlannerOutputValidationError,
    PlannerRefusalError,
    PlannerTurnContext,
)
from app.agents.planner_adapter import OpenAICompatiblePlannerAgent
from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    get_capability_registry,
)
from app.domain.models import AgentState, Component
from app.domain.planner import PlannerDecision, PlannerStatus


class SequencePlannerProvider:
    """依次返回 PlannerDecision 或抛出预设异常，并记录每次消息。

    该替身不会解析 Prompt 或生成业务答案；序列耗尽时显式失败，使测试能发现多余模型调用。
    calls 保存不可变消息元组，用于验证修复轮新增 assistant/user 而不修改初始 system/user。
    """

    def __init__(self, outcomes: list[PlannerDecision | Exception]) -> None:
        """复制预设结果序列并初始化空调用记录，避免外部列表被就地消费。

        每个 outcome 要么是已校验决策，要么是适配层预期异常；构造不执行 I/O。空序列仅用于
        断言 Agent 不应调用 Provider 的特殊测试，否则 complete 会抛出 AssertionError。
        """

        self._outcomes = list(outcomes)
        self.calls: list[tuple[ChatMessage, ...]] = []

    async def complete(self, messages: tuple[ChatMessage, ...]) -> PlannerDecision:
        """记录消息并消费一个预设结果，异常按原类型抛出。

        输入不被修改；若调用次数超出序列则说明修复预算或控制流错误。返回决策已经通过 Pydantic，
        因而测试关注 Agent 编排而不是重复验证领域 Schema。
        """

        self.calls.append(messages)
        if not self._outcomes:
            raise AssertionError("Planner provider was called more times than expected")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _planner_context(user_query: str) -> PlannerTurnContext:
    """构造活动能力与状态完全一致的 LTS 单组件 PlannerTurnContext。

    使用真实 registry 避免测试通过伪造 capability 绕过 Renderer 校验；返回值不含证据或历史
    案例，足以验证 Provider 调用与修复消息控制流。
    """

    selection = get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        )
    )
    return PlannerTurnContext(
        state=AgentState(
            run_id="run_adapter_unit_001",
            session_id="session_adapter_unit_001",
            user_query=user_query,
            intent=selection.intent.value,
            active_capabilities=[name.value for name in selection.active_capabilities],
        ),
        capabilities=selection,
        max_react_steps=6,
        remaining_time_ms=30_000,
    )


def _finish() -> PlannerDecision:
    """构造一个无 Action 且带公开停止原因的合法 finish 决策。

    辅助函数使用领域模型而非字典，让各测试共享相同成功结果；它不包含证据或根因，只证明
    Provider 输出通过后能被 Agent 原样返回。
    """

    return PlannerDecision(
        status=PlannerStatus.FINISH,
        decision_summary="结构化决策有效。",
        stop_reason="evidence_sufficient",
    )


def _invalid(raw_output: str = "not-json") -> PlannerOutputValidationError:
    """构造带原输出和字段摘要的首次 Planner Schema 失败。

    raw_output 仅用于下一轮 assistant 回放，不进入异常字符串；固定摘要便于断言修复 user 消息。
    默认 attempts=1，第二次失败由 Agent 转换为 attempts=2。
    """

    return PlannerOutputValidationError(
        validation_summary="root: Invalid JSON",
        raw_output=raw_output,
    )


@pytest.mark.asyncio
async def test_valid_decision_returns_without_repair() -> None:
    """验证首次 Structured Output 合法时只调用 Provider 一次并原样返回。

    消息必须只有 system/user 两项，修复预算不会被预先消费；结果身份相同证明 Agent 没有重写
    PlannerDecision 或添加未经模型输出的 Action/Observation。
    """

    decision = _finish()
    provider = SequencePlannerProvider([decision])
    agent = OpenAICompatiblePlannerAgent(provider=provider, repair_count=1)

    result = await agent.decide(_planner_context("检查 LTS"))

    assert result is decision
    assert len(provider.calls) == 1
    assert [message.role.value for message in provider.calls[0]] == ["system", "user"]


@pytest.mark.asyncio
async def test_invalid_first_output_gets_exactly_one_schema_repair() -> None:
    """验证首次 JSON 无效时追加 assistant 原输出和最小修复 user 指令。

    第二次合法结果结束调用；修复消息必须说明 Pydantic 错误且禁止新增事实/Thought。初始两条
    消息保持不变，证明修复没有替换版本化 system Prompt。
    """

    provider = SequencePlannerProvider([_invalid(), _finish()])
    agent = OpenAICompatiblePlannerAgent(provider=provider, repair_count=1)

    result = await agent.decide(_planner_context("检查 LTS"))

    assert result.status is PlannerStatus.FINISH
    assert len(provider.calls) == 2
    assert provider.calls[1][:2] == provider.calls[0]
    assert [message.role.value for message in provider.calls[1]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert provider.calls[1][2].content == "not-json"
    assert "root: Invalid JSON" in provider.calls[1][3].content
    assert "不要添加新事实" in provider.calls[1][3].content


@pytest.mark.asyncio
async def test_second_invalid_output_stops_after_two_attempts() -> None:
    """验证修复输出仍无效时抛出 attempts=2，且绝不发起第三次请求。

    最终异常字符串不包含 raw output，只公开结构化失败次数；Provider calls 恰好为二，证明修复
    预算是硬上限而不是可递归重试的建议值。
    """

    provider = SequencePlannerProvider([_invalid("first-bad"), _invalid("second-bad")])
    agent = OpenAICompatiblePlannerAgent(provider=provider, repair_count=1)

    with pytest.raises(PlannerOutputValidationError) as exc_info:
        await agent.decide(_planner_context("检查 LTS"))

    assert exc_info.value.attempts == 2
    assert exc_info.value.raw_output == "second-bad"
    assert "second-bad" not in str(exc_info.value)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_refusal_is_not_treated_as_repairable_format_error() -> None:
    """验证模型 refusal 直接传播且只调用一次 Provider。

    refusal 可能不符合 PlannerDecision Schema，但它代表安全决策而非格式错误；Agent 不应通过
    “请修复 JSON”再次请求来规避拒绝。异常公开摘要也不能复制原始策略文本。
    """

    refusal = PlannerRefusalError("provider policy detail")
    provider = SequencePlannerProvider([refusal])
    agent = OpenAICompatiblePlannerAgent(provider=provider, repair_count=1)

    with pytest.raises(PlannerRefusalError) as exc_info:
        await agent.decide(_planner_context("检查 LTS"))

    assert exc_info.value.refusal == "provider policy detail"
    assert "provider policy detail" not in str(exc_info.value)
    assert len(provider.calls) == 1
