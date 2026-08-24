"""验证 `api-auth:v1` 的令牌比较、限流窗口和半配置拒绝行为。

测试全部围绕 `ApiSecurityGuard` 与 `SlidingWindowRateLimiter` 的纯逻辑，不启动 FastAPI，也不
sleep 真实时间：注入可控时钟才能确定性地断言窗口边界与 `Retry-After`。路由级强制点由
`tests/integration/test_api_authentication.py` 覆盖，两层分开是因为"判定是否正确"和"是否真的挂在
请求路径上"是两种不同的回归。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.api.security import (
    API_AUTH_CONTRACT_ID,
    MAX_TRACKED_IDENTITIES,
    MINIMUM_API_TOKEN_CHARS,
    PROTECTED_PATH_PREFIXES,
    ApiSecurityGuard,
    SlidingWindowRateLimiter,
    is_protected_path,
)

VALID_TOKEN = "t" * MINIMUM_API_TOKEN_CHARS


class ManualClock:
    """提供可手动推进的单调时钟，使限流窗口断言不依赖真实等待。

    限流的关键行为发生在窗口边界上；用 sleep 复现既慢又不稳定。该替身只暴露 `advance` 和调用
    协议，保证测试推进的时间量与被测代码看到的完全一致。
    """

    def __init__(self) -> None:
        """从 0 开始计时，避免断言里出现与机器启动时间相关的绝对数值。

        虚拟起点为 0 让"窗口剩余秒数"这类断言可以写成字面量；如果沿用真实 monotonic 起点，
        同一份断言在不同机器上会得到不同的数字。
        """

        self.now = 0.0

    def __call__(self) -> float:
        """返回当前虚拟时刻，签名与 `time.monotonic` 一致以便直接注入限流器。

        保持可调用而不是提供 `now()` 方法，是为了让被测代码完全不知道自己在被测试——注入点就是
        生产代码里的同一个 `clock` 参数，不存在只在测试路径生效的分支。
        """

        return self.now

    def advance(self, seconds: float) -> None:
        """把虚拟时钟向前推进给定秒数，模拟窗口内与跨窗口两类请求节奏。

        只允许向前推进，因为被测限流器依赖单调性；提供负值会构造出生产环境不可能出现的状态，
        测试通过与否都无法说明真实行为。
        """

        self.now += seconds


def test_contract_and_protected_prefixes_are_pinned() -> None:
    """锁定契约 ID 与受保护前缀集合，防止保护范围被静默缩小。

    前缀是 fail-closed 设计的唯一依据：如果有人把 `/api/v1` 移出集合，所有诊断路由会在没有任何
    测试报警的情况下变成公开接口。`/health` 必须保持公开，否则容器存活探针需要凭据。
    """

    assert API_AUTH_CONTRACT_ID == "api-auth:v1"
    assert PROTECTED_PATH_PREFIXES == ("/api/v1", "/metrics")
    assert is_protected_path("/api/v1/sessions") is True
    assert is_protected_path("/api/v1/runs/run-1/trace") is True
    assert is_protected_path("/metrics") is True
    assert is_protected_path("/health") is False
    assert is_protected_path("/demo") is False
    assert is_protected_path("/demo/static/app.js") is False


def test_disabled_mode_allows_requests_but_still_rate_limits() -> None:
    """验证关闭鉴权时请求仍受配额约束，且不需要任何 Authorization 头。

    默认演示部署没有令牌，但"无需令牌"不等于"可以无限调用付费模型"；限流必须在 disabled 模式下
    同样生效，否则唯一的成本保护只剩下 ReAct 步数上限。
    """

    clock = ManualClock()
    guard = ApiSecurityGuard(
        mode="disabled",
        token=None,
        max_requests=2,
        window_seconds=60,
        clock=clock,
    )

    assert guard.mode == "disabled"
    assert guard.authorize(authorization_header=None, client_host="10.0.0.1") is None
    assert guard.authorize(authorization_header=None, client_host="10.0.0.1") is None
    rejection = guard.authorize(authorization_header=None, client_host="10.0.0.1")

    assert rejection is not None
    assert rejection.status_code == 429
    assert rejection.error_code == "rate_limited"
    assert rejection.headers == {"Retry-After": "60"}


def test_bearer_mode_accepts_exact_token_and_rejects_variants_identically() -> None:
    """验证只有精确令牌放行，缺失/错误方案/错误令牌返回完全相同的 401。

    响应体和响应头必须逐字相同：任何差异都会把"该实例是否配置了令牌"变成可探测信息，甚至帮助
    攻击者确认令牌前缀。`WWW-Authenticate` 仍要返回，因为它是 HTTP 协议要求而不是实例指纹。
    """

    clock = ManualClock()
    guard = ApiSecurityGuard(
        mode="bearer",
        token=SecretStr(VALID_TOKEN),
        max_requests=50,
        window_seconds=60,
        clock=clock,
    )

    assert guard.authorize(
        authorization_header=f"Bearer {VALID_TOKEN}",
        client_host="10.0.0.2",
    ) is None
    # scheme 大小写不敏感是 RFC 7235 的要求，不是宽松解析。
    assert guard.authorize(
        authorization_header=f"bearer {VALID_TOKEN}",
        client_host="10.0.0.2",
    ) is None

    wrong_token = "x" * MINIMUM_API_TOKEN_CHARS
    rejections = [
        guard.authorize(authorization_header=None, client_host="10.0.0.2"),
        guard.authorize(authorization_header="", client_host="10.0.0.2"),
        guard.authorize(authorization_header=f"Token {VALID_TOKEN}", client_host="10.0.0.2"),
        guard.authorize(authorization_header=f"Bearer {wrong_token}", client_host="10.0.0.2"),
        guard.authorize(authorization_header=f"Bearer {VALID_TOKEN}extra", client_host="10.0.0.2"),
        guard.authorize(authorization_header="Bearer", client_host="10.0.0.2"),
    ]

    assert all(rejection is not None for rejection in rejections)
    assert {rejection.status_code for rejection in rejections} == {401}
    assert {rejection.error_code for rejection in rejections} == {"unauthorized"}
    assert len({rejection.message for rejection in rejections}) == 1
    assert all(
        rejection.headers == {"WWW-Authenticate": 'Bearer realm="dataops-api"'}
        for rejection in rejections
    )


def test_rate_limit_precedes_authentication_so_token_guessing_is_throttled() -> None:
    """验证限流在鉴权之前生效，使错误令牌的暴力尝试也会撞上配额。

    如果顺序相反，401 会在限流之前返回，猜令牌就完全不受配额约束——这是鉴权本身最容易被绕过的
    实现细节，因此用一个独立测试固定顺序而不是依赖代码阅读。
    """

    clock = ManualClock()
    guard = ApiSecurityGuard(
        mode="bearer",
        token=SecretStr(VALID_TOKEN),
        max_requests=2,
        window_seconds=30,
        clock=clock,
    )

    first = guard.authorize(authorization_header="Bearer wrong", client_host="10.0.0.3")
    second = guard.authorize(authorization_header="Bearer wrong", client_host="10.0.0.3")
    third = guard.authorize(authorization_header="Bearer wrong", client_host="10.0.0.3")

    assert first is not None and first.status_code == 401
    assert second is not None and second.status_code == 401
    assert third is not None and third.status_code == 429
    # 即使随后送上正确令牌，配额仍在窗口内生效：限流保护的是资源而不是身份。
    limited = guard.authorize(
        authorization_header=f"Bearer {VALID_TOKEN}",
        client_host="10.0.0.3",
    )
    assert limited is not None and limited.status_code == 429


def test_rate_limit_is_per_identity_and_recovers_after_the_window() -> None:
    """验证配额按来源身份独立计数，并在滑动窗口滚出后自动恢复。

    两个断言合成一个测试是因为它们描述同一条不变量的两面：一个来源打满不应影响其它来源，而被限
    的来源必须能在窗口长度内恢复，否则限流等于永久封禁。
    """

    clock = ManualClock()
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=10, clock=clock)

    assert limiter.check("ip:a") is None
    assert limiter.check("ip:a") is None
    assert limiter.check("ip:a") == 10
    # 另一个来源拥有独立窗口，因此一个客户端的突发不会把其它客户端一起挡在门外。
    assert limiter.check("ip:b") is None

    clock.advance(4)
    assert limiter.check("ip:a") == 6
    clock.advance(6.1)
    assert limiter.check("ip:a") is None


def test_unknown_client_host_falls_back_to_a_single_anonymous_bucket() -> None:
    """验证来源不可知时仍然限流，而不是因为缺少 IP 就放弃配额。

    某些传输（内存测试传输、部分反向代理配置）不提供 client 地址；把它们归入一个显式匿名桶会牺牲
    精度，但保证"没有 IP"不能成为绕过配额的方法。
    """

    clock = ManualClock()
    guard = ApiSecurityGuard(
        mode="disabled",
        token=None,
        max_requests=1,
        window_seconds=5,
        clock=clock,
    )

    assert guard.identify(None) == "ip:unknown"
    assert guard.identify("") == "ip:unknown"
    assert guard.identify("10.0.0.9") == "ip:10.0.0.9"
    assert guard.authorize(authorization_header=None, client_host=None) is None
    assert guard.authorize(authorization_header=None, client_host=None) is not None


def test_limiter_evicts_cold_identities_to_keep_memory_bounded() -> None:
    """验证追踪身份数超过上限后按 LRU 淘汰，避免伪造来源把限流表撑爆。

    限流表的键来自不可信输入，因此它本身就是一个内存放大面；测试用超过上限的身份数量证明表大小
    收敛，而不是仅依赖代码里的一句注释。
    """

    clock = ManualClock()
    limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60, clock=clock)

    for index in range(MAX_TRACKED_IDENTITIES + 50):
        assert limiter.check(f"ip:10.1.{index // 256}.{index % 256}") is None

    assert len(limiter._hits) == MAX_TRACKED_IDENTITIES


@pytest.mark.parametrize(
    ("mode", "token", "message_fragment"),
    [
        ("bearer", None, "requires an api auth token"),
        ("disabled", SecretStr(VALID_TOKEN), "must be unset"),
        ("bearer", SecretStr("short-token"), "at least"),
        ("bearer", SecretStr("with space " + "a" * MINIMUM_API_TOKEN_CHARS), "printable ASCII"),
        ("bearer", SecretStr("令" * MINIMUM_API_TOKEN_CHARS), "printable ASCII"),
    ],
)
def test_guard_refuses_half_configured_or_weak_authentication(
    mode: str,
    token: SecretStr | None,
    message_fragment: str,
) -> None:
    """验证半配置、弱令牌和非 ASCII 令牌都在构造阶段失败，而不是运行期才暴露。

    守卫在 lifespan 构造，因此这些错误等价于"拒绝开放端口"。`disabled` 却配了令牌同样要拒绝：
    部署者会据此以为接口已受保护。带空格或 CJK 的令牌不能安全进入 HTTP 头，放宽会造成"看起来
    配好了、实际永不匹配"的实例。
    """

    with pytest.raises(ValueError, match=message_fragment):
        ApiSecurityGuard(
            mode=mode,  # type: ignore[arg-type]
            token=token,
            max_requests=10,
            window_seconds=60,
        )


def test_guard_rejects_unsupported_mode_and_illegal_quota() -> None:
    """验证未知鉴权模式与非法配额参数都会立即抛错，避免静默降级为放行。

    未知模式若被当成 disabled 处理，一次拼写错误就会关掉整个鉴权；配额为零或窗口非正会让限流
    要么永远拒绝要么除零，两者都必须在启动阶段失败。
    """

    with pytest.raises(ValueError, match="unsupported api auth mode"):
        ApiSecurityGuard(mode="basic", token=None, max_requests=10, window_seconds=60)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_requests"):
        SlidingWindowRateLimiter(max_requests=0, window_seconds=60)
    with pytest.raises(ValueError, match="window_seconds"):
        SlidingWindowRateLimiter(max_requests=10, window_seconds=0)
