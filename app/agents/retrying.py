"""在 Provider 边界为 Planner/Auditor 模型调用提供有界瞬时重试与指数退避。

重试放在这一层而不是塞进 `OpenAICompatiblePlannerProvider.complete` 内部，是为了保住两条已有
边界：具体 Provider 继续保持 `max_retries=0` 且一次 `complete` 只发一次网络请求，因此每次尝试
仍各自产生一条 `model-call-metric:v1` 与一个 `model_call` span——"第一次 429、第二次成功"在遥测里
是两条可归因的记录，而不是被平均掉的一条。包装器只做确定性控制流，不改写消息、不降级、不吞错。

与 MCP 侧 `McpToolExecutor` 的策略刻意对称：只有供应商已判定为瞬时的失败才重复同一次调用，
非瞬时失败（认证、内容过滤、Schema 不合法）立即上抛。Schema 修复仍归 Agent 适配层的
`repair_count`，两者互不干扰——传输失败不该消耗格式修复预算，格式错误也不该触发退避等待。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from app.agents.auditor import AuditorProviderError
from app.agents.auditor_chat import AuditorChatProvider
from app.agents.chat import ChatMessage, PlannerChatProvider
from app.agents.planner import PlannerProviderError
from app.domain.models import AuditResult
from app.domain.planner import PlannerDecision

MODEL_TRANSIENT_RETRY_CONTRACT_ID = "model-transient-retry:v1"


class TransientRetryPolicy(BaseModel):
    """描述一次模型调用允许的总尝试次数与指数退避节奏。

    上限刻意压到三次尝试：真实实测的失败形态是端点在约一分钟内被打满配额后连续拒绝，重试三次
    以上既救不回这种窗口，又会把单次决策的最坏耗时推到超出 ReAct 墙钟预算。退避从秒级起步而不是
    毫秒级，因为 429 的恢复粒度由网关的计费窗口决定，几十毫秒的重试只是再撞一次同一道墙。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=2, ge=1, le=3)
    initial_backoff_seconds: float = Field(default=1.0, gt=0, le=30)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    max_backoff_seconds: float = Field(default=8.0, gt=0, le=60)

    def delay_for_attempt(self, attempt: int) -> float:
        """返回第 attempt 次尝试失败后、进入下一次尝试前应当等待的秒数。

        attempt 从 1 开始计数，因此首次失败使用 initial_backoff_seconds。退避按倍数增长但被
        max_backoff_seconds 截断，保证最坏等待时间可以在配置阶段算出来而不是运行时才发现。
        故意不加随机抖动：本系统的并发度是单个诊断 run，抖动只会让实测耗时不可复现。
        """

        if attempt < 1:
            raise ValueError("attempt must be a positive integer")
        grown = self.initial_backoff_seconds * self.backoff_multiplier ** (attempt - 1)
        return min(grown, self.max_backoff_seconds)

    def worst_case_added_seconds(self, single_call_timeout_seconds: float) -> float:
        """算出重试相对"不重试"最多额外增加的墙钟秒数，供预算校验与文档引用。

        额外开销等于所有重试尝试各自可能跑满的超时，加上每次尝试之间的退避等待。调用方用它确认
        ReAct 墙钟预算至少还容得下一次瞬时故障，否则一次本可恢复的 429 会被预算截断成
        total_timeout，那等于把重试加了又不让它生效。
        """

        if single_call_timeout_seconds <= 0:
            raise ValueError("single_call_timeout_seconds must be positive")
        retries = self.max_attempts - 1
        backoff = sum(self.delay_for_attempt(attempt) for attempt in range(1, retries + 1))
        return retries * single_call_timeout_seconds + backoff


class RetryingPlannerChatProvider:
    """为 Planner 结构化决策调用补上有界瞬时重试，其余失败语义保持不变。

    只捕获 PlannerProviderError 且只在其 retryable 为真时重试：该标记由 Provider 依据 429/5xx、
    超时和连接失败设置，认证失败（401/403）在同一分类里被显式排除，因为重复发送坏凭据既救不回
    调用也会加速触发网关封禁。PlannerOutputValidationError 与 PlannerRefusalError 是兄弟异常而不是
    子类，因此天然不会被这里吞掉。
    """

    def __init__(
        self,
        inner: PlannerChatProvider,
        *,
        policy: TransientRetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """包装底层 Provider 并注入可替换的等待函数以便测试断言退避序列。

        sleep 默认是 asyncio.sleep；单元测试注入记录型替身即可在毫秒内验证节奏，不必真的等待。
        包装器不持有 HTTP 资源，因此不提供 aclose——连接池责任仍归被包装的具体 Provider，
        由工厂返回的 runtime 容器在 lifespan 退出时精确释放。
        """

        self._inner = inner
        self._policy = policy
        self._sleep = sleep

    async def complete(self, messages: tuple[ChatMessage, ...]) -> PlannerDecision:
        """按重试预算提交同一批消息，返回首个成功的 PlannerDecision。

        消息序列逐次原样重发：重试的前提正是"这次调用本身没有产生任何副作用"，改写内容会让第二次
        尝试变成另一个语义不同的请求。最后一次尝试失败时原样上抛，让 ReAct 循环照旧以
        planner_provider_error 收口，而不是把耗尽预算伪装成模型拒绝。
        """

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await self._inner.complete(messages)
            except PlannerProviderError as error:
                # 非瞬时失败或预算用尽都必须立刻上抛：继续重试只会推迟同一个终态，还会白烧墙钟预算。
                if not error.retryable or attempt >= self._policy.max_attempts:
                    raise
                await self._sleep(self._policy.delay_for_attempt(attempt))
        raise RuntimeError("planner transient retry loop exited without a decision")


class RetryingAuditorChatProvider:
    """为独立 Auditor 调用补上同一套有界瞬时重试，不改变审计的否决语义。

    这一层对审计阶梯尤其重要：AuditorAgentError 会让报告立即 degraded 且不消耗返工预算，因此一次
    瞬时 429 原本足以让"审计不可用"变成事实上的终态。重试只争取让审计真的跑起来，绝不因为网络
    失败而放行报告——预算耗尽后仍然照原样上抛，降级路径不变。
    """

    def __init__(
        self,
        inner: AuditorChatProvider,
        *,
        policy: TransientRetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """包装底层审计 Provider，并与 Planner 侧共用同一份策略对象与等待函数注入方式。

        两个角色共用策略是有意的：它们打同一个端点、共享同一份配额，分别配置只会让"到底哪一层
        在退避"变得难以推断。资源关闭责任同样留在被包装的具体 Provider 上。
        """

        self._inner = inner
        self._policy = policy
        self._sleep = sleep

    async def complete(self, messages: tuple[ChatMessage, ...]) -> AuditResult:
        """按重试预算提交审计消息，返回首个成功的 AuditResult。

        与 Planner 侧完全同构，只是异常类型换成 AuditorProviderError；保持两段代码显式并列而不是
        抽象成泛型，是因为两者的失败后果不同（一个停止取证、一个触发降级），未来很可能各自演化。
        """

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await self._inner.complete(messages)
            except AuditorProviderError as error:
                # 认证失败重试没有意义，预算耗尽也必须让降级路径照常接管，二者都直接上抛。
                if not error.retryable or attempt >= self._policy.max_attempts:
                    raise
                await self._sleep(self._policy.delay_for_attempt(attempt))
        raise RuntimeError("auditor transient retry loop exited without a result")
