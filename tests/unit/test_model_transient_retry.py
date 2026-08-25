"""验证模型侧有界瞬时重试的退避节奏、终态语义与非瞬时失败的立即上抛。

测试全部注入记录型 sleep 与脚本化 Provider 替身，不发任何网络请求，因此可以在毫秒内断言
"第几次尝试、等了多久、最终抛什么"。重点不是"能重试"，而是三条边界：认证失败不重试、预算耗尽
后原样上抛（不把耗尽伪装成拒绝或格式错误）、Schema/refusal 失败完全不进入退避路径。
"""

import pytest
from pydantic import ValidationError

from app.agents.auditor import (
    AuditorOutputValidationError,
    AuditorProviderError,
    AuditorRefusalError,
)
from app.agents.chat import ChatMessage, ChatRole
from app.agents.planner import (
    PlannerOutputValidationError,
    PlannerProviderError,
    PlannerRefusalError,
)
from app.agents.retrying import (
    MODEL_TRANSIENT_RETRY_CONTRACT_ID,
    RetryingAuditorChatProvider,
    RetryingPlannerChatProvider,
    TransientRetryPolicy,
)
from app.domain.models import AuditResult, AuditStatus
from app.domain.planner import PlannerDecision, PlannerStatus

MESSAGES = (
    ChatMessage(role=ChatRole.SYSTEM, content="系统提示"),
    ChatMessage(role=ChatRole.USER, content="用户输入"),
)


class RecordingSleep:
    """记录每次退避等待秒数的替身，替代 asyncio.sleep 以便断言精确节奏。

    只保存调用顺序而不真的挂起事件循环，因此测试既不会变慢，也能证明"重试之间确实等待过"。
    列表按调用顺序追加，断言可以直接比较整段退避序列而不是逐次断言。
    """

    def __init__(self) -> None:
        """初始化空的等待记录列表，使替身在首次调用前即可安全断言退避序列。

        不接收参数是有意的：策略对象已经决定了等待时长，替身只负责观察而不参与决策，
        否则测试就会把被测逻辑的一部分搬到断言工具里，失去证明力。
        """

        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """记录一次请求的等待秒数并立即返回，不产生真实时间开销。

        seconds 由 TransientRetryPolicy.delay_for_attempt 计算；替身不校验其取值，
        以免把被测逻辑的断言搬进替身内部。
        """

        self.delays.append(seconds)


class ScriptedPlannerProvider:
    """按脚本依次抛出异常或返回决策的 Planner Provider 替身。

    脚本是一个列表，每次 complete 消费一项：异常实例被抛出，PlannerDecision 被返回。
    同时记录收到的消息序列，用于证明重试逐次原样重发同一批消息而不是改写内容。
    """

    def __init__(self, script: list[object]) -> None:
        """保存待消费的脚本项，并初始化调用次数与收到的消息记录。

        script 顺序即尝试顺序；脚本耗尽后再被调用会显式失败，防止测试悄悄依赖预算之外的尝试。
        消息记录用于证明重试原样重发同一批内容，而不是被包装层改写成第二轮请求。
        """

        self._script = list(script)
        self.calls = 0
        self.seen: list[tuple[ChatMessage, ...]] = []

    async def complete(self, messages: tuple[ChatMessage, ...]) -> PlannerDecision:
        """消费下一个脚本项，抛出异常或返回一个合法 PlannerDecision。

        messages 被原样记录以便断言重发内容一致；脚本耗尽时抛 AssertionError，
        表示被测重试逻辑发起了预算之外的尝试。
        """

        self.calls += 1
        self.seen.append(messages)
        if not self._script:
            raise AssertionError("scripted planner provider was called more times than expected")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, PlannerDecision)
        return item


class ScriptedAuditorProvider:
    """按脚本依次抛出异常或返回审计结果的 Auditor Provider 替身。

    与 Planner 替身同构，只是返回类型换成 AuditResult；保持两个替身并列而不抽象共同基类，
    是为了让每个测试读起来仍然是"这个角色发生了什么"，而不需要跳转到共享工具类。
    """

    def __init__(self, script: list[object]) -> None:
        """保存脚本项并初始化调用计数，供断言实际发生了几次审计尝试使用。

        脚本项与 Planner 侧规则一致：异常抛出、AuditResult 返回、耗尽即视为测试失败。
        不记录消息是有意的：消息一致性由 Planner 侧同构测试覆盖，这里只关注尝试次数与终态。
        """

        self._script = list(script)
        self.calls = 0

    async def complete(self, messages: tuple[ChatMessage, ...]) -> AuditResult:
        """消费下一个脚本项，抛出异常或返回一个合法 AuditResult。

        messages 参数保留以满足协议签名；审计侧的断言集中在尝试次数与最终异常类型上，
        消息内容一致性已由 Planner 侧同构测试覆盖。
        """

        self.calls += 1
        if not self._script:
            raise AssertionError("scripted auditor provider was called more times than expected")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, AuditResult)
        return item


def _retryable_planner_error() -> PlannerProviderError:
    """构造一个被供应商判定为瞬时的 Planner 失败（等价于 HTTP 429）。

    retryable 为真是重试的唯一准入条件；error_code 只用于受控日志与断言，公开摘要不含端点信息。
    """

    return PlannerProviderError(
        error_code="rate_limited",
        public_summary="OpenAI-compatible Planner 服务返回 HTTP 429。",
        retryable=True,
    )


def _authentication_planner_error() -> PlannerProviderError:
    """构造一个不可重试的 Planner 认证失败（等价于 HTTP 401/403）。

    重复发送坏凭据既救不回调用也会加速触发网关封禁，因此这类失败必须一次即上抛。
    """

    return PlannerProviderError(
        error_code="authentication_error",
        public_summary="OpenAI-compatible Planner 服务返回 HTTP 401。",
        retryable=False,
    )


def test_policy_rejects_unbounded_budget_and_computes_worst_case_cost() -> None:
    """验证策略上限被 Schema 钉死，且最坏额外墙钟开销可在配置阶段算出。

    max_attempts 超过三次直接是 ValidationError，因为实测的配额窗口打满形态重试更多次救不回来，
    只会把单次决策的最坏耗时推出 ReAct 墙钟预算。worst_case_added_seconds 把"重试要多花多少时间"
    变成可校验的数字，Settings 正是用它保证预算容得下一次瞬时故障。
    """

    with pytest.raises(ValidationError):
        TransientRetryPolicy(max_attempts=4)
    with pytest.raises(ValidationError):
        TransientRetryPolicy(initial_backoff_seconds=0)

    policy = TransientRetryPolicy()
    assert MODEL_TRANSIENT_RETRY_CONTRACT_ID == "model-transient-retry:v1"
    assert policy.max_attempts == 2
    assert [policy.delay_for_attempt(attempt) for attempt in (1, 2, 3)] == [1.0, 2.0, 4.0]
    # 单次尝试超时 30s、只重试一次，因此最坏额外开销是一次跑满的超时加一次 1s 退避。
    assert policy.worst_case_added_seconds(30.0) == 31.0
    assert TransientRetryPolicy(max_attempts=3).worst_case_added_seconds(30.0) == 63.0
    with pytest.raises(ValueError):
        policy.delay_for_attempt(0)
    with pytest.raises(ValueError):
        policy.worst_case_added_seconds(0)


def test_backoff_is_capped_so_worst_case_stays_computable() -> None:
    """验证退避按倍数增长但被 max_backoff_seconds 截断，最坏等待不随尝试次数发散。

    截断是预算校验能成立的前提：只要退避有上界，最坏总开销就是"重试数×超时 + 有界退避和"，
    可以在启动阶段与 react_total_timeout_seconds 比较，而不是等到线上才发现预算被吃穿。
    """

    policy = TransientRetryPolicy(
        max_attempts=3,
        initial_backoff_seconds=4.0,
        backoff_multiplier=10.0,
        max_backoff_seconds=5.0,
    )

    assert policy.delay_for_attempt(1) == 4.0
    assert policy.delay_for_attempt(2) == 5.0
    assert policy.delay_for_attempt(5) == 5.0


@pytest.mark.asyncio
async def test_planner_retry_recovers_transient_failure_and_resends_same_messages() -> None:
    """验证一次瞬时 429 后重试成功，且第二次尝试重发完全相同的消息序列。

    重试的前提正是"这次调用没有产生任何副作用"，改写内容会让第二次尝试变成语义不同的请求，
    从而把重试悄悄变成第二轮决策。断言退避恰好等待一次，证明包装层没有在成功后多睡一轮。
    """

    decision = PlannerDecision(
        status=PlannerStatus.FINISH,
        decision_summary="重试后返回有效决策。",
        stop_reason="evidence_sufficient",
    )
    inner = ScriptedPlannerProvider([_retryable_planner_error(), decision])
    sleep = RecordingSleep()
    provider = RetryingPlannerChatProvider(
        inner,
        policy=TransientRetryPolicy(),
        sleep=sleep,
    )

    result = await provider.complete(MESSAGES)

    assert result is decision
    assert inner.calls == 2
    assert inner.seen[0] == inner.seen[1] == MESSAGES
    assert sleep.delays == [1.0]


@pytest.mark.asyncio
async def test_planner_retry_reraises_authentication_failure_without_waiting() -> None:
    """验证不可重试的认证失败一次即上抛，且完全不进入退避等待。

    这条边界保证坏凭据不会被反复投递：既不浪费墙钟预算，也不会让网关把本机判成暴力尝试。
    上抛的仍是原异常对象，因此 ReAct 循环照旧以 planner_provider_error 收口。
    """

    error = _authentication_planner_error()
    inner = ScriptedPlannerProvider([error])
    sleep = RecordingSleep()
    provider = RetryingPlannerChatProvider(inner, policy=TransientRetryPolicy(), sleep=sleep)

    with pytest.raises(PlannerProviderError) as raised:
        await provider.complete(MESSAGES)

    assert raised.value is error
    assert inner.calls == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_planner_retry_exhausts_budget_and_surfaces_last_provider_error() -> None:
    """验证预算耗尽后原样上抛最后一次失败，不把耗尽伪装成别的终态。

    最坏情况必须仍然是 planner_provider_error：若包装层在这里改写异常类型或吞掉失败，
    上层就会把"请求根本没打到模型"当成模型给出了结论。退避次数等于尝试数减一，不多睡一次。
    """

    last = _retryable_planner_error()
    inner = ScriptedPlannerProvider([_retryable_planner_error(), _retryable_planner_error(), last])
    sleep = RecordingSleep()
    provider = RetryingPlannerChatProvider(
        inner,
        policy=TransientRetryPolicy(max_attempts=3),
        sleep=sleep,
    )

    with pytest.raises(PlannerProviderError) as raised:
        await provider.complete(MESSAGES)

    assert raised.value is last
    assert raised.value.stop_reason == "planner_provider_error"
    assert inner.calls == 3
    assert sleep.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_planner_retry_never_swallows_schema_or_refusal_failures() -> None:
    """验证 Schema 失败与结构化拒绝不进入重试路径，两套预算互不干扰。

    传输失败不该消耗格式修复预算，格式错误也不该触发退避等待——修复仍归 Agent 适配层的
    repair_count。拒绝更不能靠重发规避：那等于用重试绕过供应商的安全判断。
    """

    validation = PlannerOutputValidationError(
        validation_summary="root: Invalid JSON",
        raw_output="not-json",
    )
    sleep = RecordingSleep()
    invalid_inner = ScriptedPlannerProvider([validation])
    provider = RetryingPlannerChatProvider(
        invalid_inner,
        policy=TransientRetryPolicy(max_attempts=3),
        sleep=sleep,
    )

    with pytest.raises(PlannerOutputValidationError):
        await provider.complete(MESSAGES)
    assert invalid_inner.calls == 1

    refusal_inner = ScriptedPlannerProvider([PlannerRefusalError("policy")])
    provider = RetryingPlannerChatProvider(
        refusal_inner,
        policy=TransientRetryPolicy(max_attempts=3),
        sleep=sleep,
    )

    with pytest.raises(PlannerRefusalError):
        await provider.complete(MESSAGES)
    assert refusal_inner.calls == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_auditor_retry_recovers_before_degrade_ladder_takes_over() -> None:
    """验证审计侧同样能靠一次重试跑起来，避免瞬时故障直接变成"审计不可用"。

    AuditorAgentError 会让报告立即 degraded 且不消耗返工预算，因此一次 429 原本就足以让降级
    成为事实终态。重试只争取让审计真的执行，返回的仍是模型自己的 accept/revise 决策。
    """

    accepted = AuditResult(status=AuditStatus.ACCEPT)
    inner = ScriptedAuditorProvider(
        [
            AuditorProviderError(
                error_code="service_error",
                public_summary="OpenAI-compatible Auditor 服务返回 HTTP 503。",
                retryable=True,
            ),
            accepted,
        ]
    )
    sleep = RecordingSleep()
    provider = RetryingAuditorChatProvider(inner, policy=TransientRetryPolicy(), sleep=sleep)

    result = await provider.complete(MESSAGES)

    assert result is accepted
    assert inner.calls == 2
    assert sleep.delays == [1.0]


@pytest.mark.asyncio
async def test_auditor_retry_keeps_degrade_path_intact_when_budget_runs_out() -> None:
    """验证审计重试耗尽后照原样上抛，降级路径与非瞬时失败语义都不被改写。

    这里是最危险的地方：如果包装层为了"让流程走下去"把失败转成一个合成 accept，报告就会因为
    网络故障而被放行。断言最终异常仍是 AuditorProviderError，并且 Schema 失败不触发退避。
    """

    last = AuditorProviderError(
        error_code="rate_limited",
        public_summary="OpenAI-compatible Auditor 服务返回 HTTP 429。",
        retryable=True,
    )
    inner = ScriptedAuditorProvider(
        [
            AuditorProviderError(
                error_code="rate_limited",
                public_summary="OpenAI-compatible Auditor 服务返回 HTTP 429。",
                retryable=True,
            ),
            last,
        ]
    )
    sleep = RecordingSleep()
    provider = RetryingAuditorChatProvider(inner, policy=TransientRetryPolicy(), sleep=sleep)

    with pytest.raises(AuditorProviderError) as raised:
        await provider.complete(MESSAGES)

    assert raised.value is last
    assert raised.value.stop_reason == "auditor_provider_error"
    assert inner.calls == 2
    assert sleep.delays == [1.0]

    schema_inner = ScriptedAuditorProvider(
        [AuditorOutputValidationError(validation_summary="root: Invalid JSON", raw_output="x")]
    )
    provider = RetryingAuditorChatProvider(schema_inner, policy=TransientRetryPolicy(), sleep=sleep)
    with pytest.raises(AuditorOutputValidationError):
        await provider.complete(MESSAGES)
    assert schema_inner.calls == 1

    refusal_inner = ScriptedAuditorProvider([AuditorRefusalError("policy")])
    provider = RetryingAuditorChatProvider(
        refusal_inner, policy=TransientRetryPolicy(), sleep=sleep
    )
    with pytest.raises(AuditorRefusalError):
        await provider.complete(MESSAGES)
    assert refusal_inner.calls == 1
