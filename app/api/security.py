"""资源 API 的鉴权与进程内限流边界（`api-auth:v1`）。

模块只做两件事：判断一次请求是否携带合法 Bearer 令牌，以及判断来源在滑动窗口内是否超过配额。
它不签发令牌、不做用户体系，也不读取数据库——单一共享令牌足以把演示实例从"任何人都能触发付费
模型调用"变成"必须显式配置才可访问"，而多用户体系会引入本项目并不需要的账号存储。

拒绝结果用不可变数据类返回而不是抛 HTTPException，因为强制点是 ASGI 中间件：中间件位于路由
之外，FastAPI 的异常处理器不会捕获那里抛出的 HTTPException，返回值语义可以让中间件与单元测试
共享同一份判定逻辑。401 对"缺令牌"和"错令牌"返回完全相同的响应体，避免把"这个实例是否配置了
令牌"变成可探测信息。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

API_AUTH_CONTRACT_ID = "api-auth:v1"

ApiAuthMode = Literal["disabled", "bearer"]

# 32 字符是 256 位随机令牌 base64 编码后的量级；低于此长度的"演示口令"在公网上等于没有鉴权，
# 因此由构造函数直接拒绝启动，而不是记一条警告日志后继续对外服务。
MINIMUM_API_TOKEN_CHARS = 32

# 限流表按来源 IP 增长，恶意流量可以伪造大量来源；LRU 上限让内存占用有界，代价是极端情况下
# 最久未活动的来源配额被提前重置——这比 API 进程被限流表本身 OOM 更可接受。
MAX_TRACKED_IDENTITIES = 4096

# 受保护路径用前缀而不是逐路由声明：新增 `/api/v1/...` 路由默认就在鉴权内（fail closed），
# 不会因为忘记加 Depends 而静默裸奔。`/health` 与 `/demo` 保持公开——前者是容器存活探针，
# 后者只是无数据的静态资源；两者都不返回诊断内容。
PROTECTED_PATH_PREFIXES = ("/api/v1", "/metrics")

UNKNOWN_CLIENT_IDENTITY = "ip:unknown"


def is_protected_path(path: str) -> bool:
    """判断请求路径是否落在需要鉴权与限流的前缀集合内。

    独立成函数让中间件、健康检查和测试引用同一份前缀定义，避免"文档说保护 /metrics、代码只保护
    /api/v1"这类漂移。判定只看前缀，不做正则，因此新增子路由不需要同步任何白名单。
    """

    return path.startswith(PROTECTED_PATH_PREFIXES)


@dataclass(frozen=True)
class ApiSecurityRejection:
    """描述一次被拒绝的请求：HTTP 状态、稳定错误码、可展示消息和响应头。

    `error_code` 是给客户端做分支的稳定标识，`message` 只包含公开语义，不透露令牌是否存在、
    配额剩余多少或服务端内部异常。`headers` 承载 `WWW-Authenticate` 与 `Retry-After`，让标准
    HTTP 客户端与浏览器 Demo 都能按协议而不是按文案来判断该重试还是该补令牌。
    """

    status_code: int
    error_code: Literal["unauthorized", "rate_limited"]
    message: str
    headers: dict[str, str]


class SlidingWindowRateLimiter:
    """按来源身份统计固定时长滑动窗口内的请求数，超限时给出建议重试秒数。

    选滑动窗口而不是固定窗口计数器，是因为固定窗口在边界处允许两倍突发（窗口末尾和下一窗口
    开头各打满一次），而本项目的配额本来就是为了保护付费模型调用与数据库连接池。窗口内时间戳
    数量天然被 `max_requests` 限制，因此单个身份的内存占用有上限，无需额外裁剪逻辑。
    """

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """校验配额参数并注入单调时钟，使限流不受系统时间回拨影响。

        显式注入 `clock` 让单元测试可以确定性地推进时间，而不是 sleep 真实秒数；默认使用
        `time.monotonic` 而不是 `time.time`，因为 NTP 校正或手动改时间会让墙钟窗口计算出负
        间隔，从而把限流器变成"永远放行"或"永远拒绝"。
        """

        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_requests = max_requests
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def max_requests(self) -> int:
        """返回窗口内允许的最大请求数，供健康检查公开当前配额。

        以只读属性暴露而不是公开内部字段，避免运行期被改写：限流配额一旦在请求处理中途变化，
        已经记录的时间戳窗口与新阈值就不再对应，会出现难以复现的放行/拒绝抖动。
        """

        return self._max_requests

    @property
    def window_seconds(self) -> float:
        """返回滑动窗口长度（秒），让健康检查与文档引用同一个已校验的配置值。

        窗口长度必须和次数一起公开：只给出"120"无法判断它是每分钟还是每秒，运维也就无法判断
        自己的抓取频率是否会被限流挡掉。同样以只读属性暴露，防止运行期修改造成窗口语义漂移。
        """

        return self._window_seconds

    def check(self, identity: str) -> int | None:
        """记录一次请求；未超限返回 None，超限返回建议的 `Retry-After` 整数秒。

        超限时不追加时间戳，否则持续重试会不断把窗口向后推、形成永久封禁；返回值向上取整并至少
        为 1 秒，因为 `Retry-After` 只接受整数秒，返回 0 会让客户端立刻重试从而放大压力。
        """

        now = self._clock()
        window = self._hits.setdefault(identity, deque())
        self._hits.move_to_end(identity)
        boundary = now - self._window_seconds
        while window and window[0] <= boundary:
            window.popleft()
        if len(window) >= self._max_requests:
            retry_after = self._window_seconds - (now - window[0])
            return max(1, math.ceil(retry_after))
        window.append(now)
        self._evict_cold_identities()
        return None

    def _evict_cold_identities(self) -> None:
        """在追踪身份数超过上限时淘汰最久未活动的条目，保证限流表内存有界。

        只有在成功记录一次请求后才淘汰，这样"被限流的来源"不会因为自己的重试把其它正常来源挤出
        表外。淘汰采用 LRU 顺序，因为伪造大量一次性 IP 的攻击流量恰好是最久未活动的那部分。
        """

        while len(self._hits) > MAX_TRACKED_IDENTITIES:
            self._hits.popitem(last=False)


class ApiSecurityGuard:
    """把配置好的鉴权模式与限流配额组合成一次请求级的放行判定。

    守卫在 lifespan 阶段构造，因此弱令牌、非法配额这类问题会阻止进程开放端口，而不是等到第一个
    请求才暴露。令牌以 SHA-256 摘要保存并用 `hmac.compare_digest` 比较：摘要让内存转储里不出现
    原文，定长比较让响应时间不随匹配前缀长度变化，避免逐字符计时侧信道。
    """

    def __init__(
        self,
        *,
        mode: ApiAuthMode,
        token: SecretStr | None,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """校验模式与令牌的组合，并建立限流器；半配置组合直接拒绝构造。

        `bearer` 缺令牌、`disabled` 却配了令牌都会抛错：后者同样危险，因为部署者会以为接口已经
        受保护。令牌还必须是无空白的可见 ASCII，这样它可以安全地进入 HTTP 头，也不会因为复制粘贴
        带入换行而变成一个"看起来配置好了、实际永不匹配"的实例。
        """

        if mode not in ("disabled", "bearer"):
            raise ValueError(f"unsupported api auth mode: {mode}")
        if mode == "bearer" and token is None:
            raise ValueError("bearer mode requires an api auth token")
        if mode == "disabled" and token is not None:
            raise ValueError("api auth token must be unset while auth is disabled")
        self._mode: ApiAuthMode = mode
        self._token_digest: bytes | None = None
        if token is not None:
            secret = token.get_secret_value()
            if len(secret) < MINIMUM_API_TOKEN_CHARS:
                raise ValueError(
                    f"api auth token must be at least {MINIMUM_API_TOKEN_CHARS} characters"
                )
            if not secret.isascii() or not secret.isprintable() or " " in secret:
                raise ValueError("api auth token must be printable ASCII without spaces")
            self._token_digest = hashlib.sha256(secret.encode("ascii")).digest()
        self._limiter = SlidingWindowRateLimiter(
            max_requests=max_requests,
            window_seconds=window_seconds,
            clock=clock,
        )

    @property
    def mode(self) -> ApiAuthMode:
        """返回当前鉴权模式，供 `/health` 公开"是否需要令牌"而不泄露令牌本身。

        只暴露模式而不暴露令牌摘要：摘要虽然不可逆，但会把"令牌是否变更过"变成可观测信号，对
        排障没有价值，却给离线字典攻击提供了校验目标。
        """

        return self._mode

    @property
    def limiter(self) -> SlidingWindowRateLimiter:
        """返回限流器，使健康检查可以公开配额而无需把内部计数表变成可写属性。

        暴露对象本身而不是复制配额数字，是为了让健康响应与实际生效的限流器永远来自同一实例，
        避免"文档/健康接口说 120，运行时其实是别的值"这种最难发现的配置漂移。
        """

        return self._limiter

    def identify(self, client_host: str | None) -> str:
        """把请求来源映射为限流身份；未知来源归入同一个显式匿名桶。

        即使鉴权通过也按 IP 计数：本项目只有一个共享令牌，若用令牌当键，一个客户端的突发会
        直接耗尽所有合法客户端的配额。ASGI scope 在某些传输（如内存测试传输）里没有 client，
        此时归入固定的 `ip:unknown` 桶，而不是放弃限流。
        """

        if not client_host:
            return UNKNOWN_CLIENT_IDENTITY
        return f"ip:{client_host}"

    def authorize(
        self,
        *,
        authorization_header: str | None,
        client_host: str | None,
    ) -> ApiSecurityRejection | None:
        """判定一次受保护请求是否放行，返回 None 表示通过，否则返回结构化拒绝。

        先限流再鉴权：如果顺序相反，错误令牌会在 401 之前被放行出限流之外，暴力猜测令牌就完全
        不受配额约束。限流对已鉴权与未鉴权请求同样生效，因为耗尽数据库连接池并不需要合法令牌。
        """

        retry_after = self._limiter.check(self.identify(client_host))
        if retry_after is not None:
            return ApiSecurityRejection(
                status_code=429,
                error_code="rate_limited",
                message="request rate limit exceeded for this client",
                headers={"Retry-After": str(retry_after)},
            )
        if self._mode == "disabled":
            return None
        if self._token_matches(authorization_header):
            return None
        # 缺失、方案错误和令牌错误共享同一响应：任何差异都会告诉探测者"这个实例配了令牌"，
        # 甚至帮助他确认令牌前缀是否正确。
        return ApiSecurityRejection(
            status_code=401,
            error_code="unauthorized",
            message="a valid bearer token is required for this endpoint",
            headers={"WWW-Authenticate": 'Bearer realm="dataops-api"'},
        )

    def _token_matches(self, authorization_header: str | None) -> bool:
        """用定长摘要比较判断 Authorization 头是否携带正确的 Bearer 令牌。

        解析保持严格：必须是 `Bearer <token>` 两段结构，scheme 大小写不敏感（RFC 7235 要求），
        令牌部分不做 strip 之外的规范化，避免"末尾多个空格也算通过"这类隐式宽松。
        """

        if self._token_digest is None or not authorization_header:
            return False
        scheme, _, candidate = authorization_header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        candidate = candidate.strip()
        if not candidate or not candidate.isascii():
            return False
        digest = hashlib.sha256(candidate.encode("ascii")).digest()
        return hmac.compare_digest(digest, self._token_digest)
