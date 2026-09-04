"""MCP 网关的 Streamable HTTP 鉴权与限流边界（`mcp-transport:v1`）。

stdio 没有网络面，因此过去不需要这一层；一旦传输换成 Streamable HTTP，九个工具就变成一个可被
扫描到的 HTTP 端点。它们虽然全部只读，暴露出去的却是整条链路的排障证据（任务状态、日志、依赖
拓扑、一致性抽样），所以网关必须 fail-closed：没有令牌就不启动，而不是先跑起来再指望没人扫到。

本模块刻意不实现任何鉴权算法，而是复用资源 API 的 `ApiSecurityGuard`：同一套 SHA-256 摘要 +
`hmac.compare_digest` 定长比较、同一套先限流后鉴权的顺序、逐字相同的 401 响应体。两处各写一份
的后果是可预期的——其中一份会先长出"调试用后门"或"顺手放宽的 scheme 解析"。
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.security import ApiSecurityGuard, ApiSecurityRejection
from app.core.settings import Settings


def build_gateway_guard(settings: Settings) -> ApiSecurityGuard:
    """按配置构造网关守卫，缺令牌直接抛错以实现 fail-closed 启动。

    这里再拒绝一次是有意的冗余：`Settings._validate_mcp_transport` 已经拦掉"HTTP 却没有令牌"，
    但本函数也可能被未来的独立入口调用，而一个"忘了配令牌就裸奔"的网关是不可接受的失败模式。
    令牌强度（≥32 位可见 ASCII）由 `ApiSecurityGuard` 自己校验，不在此处复制阈值。
    """

    if settings.mcp_auth_token is None:
        raise ValueError("streamable-http mcp transport requires an auth token")
    # 模式硬编码为 bearer，网关不提供资源 API 那样的 disabled 档：那一档存在的理由是"本地演示要
    # 开箱可用"，而网关端口一旦被监听就没有"本地演示"这种低风险语境。
    return ApiSecurityGuard(
        mode="bearer",
        token=settings.mcp_auth_token,
        max_requests=settings.mcp_rate_limit_requests,
        window_seconds=settings.mcp_rate_limit_window_seconds,
    )


class McpGatewaySecurityMiddleware:
    """在 MCP Streamable HTTP 应用之前强制令牌与配额的纯 ASGI 中间件。

    写成纯 ASGI 而不是 Starlette `BaseHTTPMiddleware`：后者会把响应包进一个额外的任务并缓冲流式
    响应，而 MCP 的 Streamable HTTP 正是靠 SSE 长流返回结果的。中间件保护该应用的全部 HTTP 路径
    （不是某个前缀）——网关只有一个用途，任何路径上的匿名请求都没有正当理由。
    """

    def __init__(self, app: ASGIApp, guard: ApiSecurityGuard) -> None:
        """包装下游 ASGI 应用并保存已构造的安全守卫。

        守卫在进程启动时构造完毕，因此弱令牌或非法配额会阻止端口被监听，而不是等第一个请求才
        暴露。中间件本身无状态（计数表在守卫内），可以安全地被多个并发请求同时进入。
        """

        self._app = app
        self._guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """对 HTTP 请求执行放行判定，其余 scope 类型原样透传给下游应用。

        lifespan 必须透传：`streamable_http_app()` 把 `StreamableHTTPSessionManager.run()` 挂在
        自己的 lifespan 上，拦掉它会让会话管理器永不启动，网关随后对每个请求都返回 500。
        """

        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        rejection = self._guard.authorize(
            authorization_header=_read_authorization(scope),
            client_host=_read_client_host(scope),
        )
        if rejection is None:
            await self._app(scope, receive, send)
            return
        # 被拒的请求绝不往下游走：否则会话管理器会为一个未通过鉴权的请求分配 MCP 会话，
        # 让"扫端口"变成一种能消耗服务端状态的操作。
        await _send_rejection(send, rejection)


def _read_authorization(scope: Scope) -> str | None:
    """从原始 ASGI headers 中读取 Authorization 头，缺失时返回 None。

    ASGI 规定头名已小写，因此不需要大小写不敏感查找；用 latin-1 解码是 HTTP 头的规范字节语义，
    非法 UTF-8 不会在这里抛异常，而是交给守卫的严格 `Bearer <token>` 解析去拒绝。
    """

    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            return value.decode("latin-1")
    return None


def _read_client_host(scope: Scope) -> str | None:
    """提取限流所需的来源地址，未知来源返回 None 交给守卫归入匿名桶。

    容器网络里所有请求都来自同一个 api 容器地址，因此这个值实际上把配额变成网关的全局闸门——
    这正是想要的效果：闸门要保护被观测服务，而不是区分调用者。
    """

    client = scope.get("client")
    if not client:
        return None
    return client[0]


async def _send_rejection(send: Send, rejection: ApiSecurityRejection) -> None:
    """把结构化拒绝编码成 JSON 响应，字段与资源 API 的拒绝逐字一致。

    响应体沿用 `{"error": ..., "message": ...}` 而不是 JSON-RPC 错误对象：拒绝发生在 MCP 会话建立
    之前，此时还没有 request id 可以回填，伪造一个 JSON-RPC 错误反而会让客户端以为协议层已经握手
    成功。MCP SDK 只看 HTTP 状态码，因此 401 会如期变成分类后的 PERMISSION_DENIED。
    """

    body = json.dumps(
        {"error": rejection.error_code, "message": rejection.message},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    # 守卫给的头（WWW-Authenticate、Retry-After）追加在后面而不是先写：纯 ASGI 没有框架帮忙合并
    # 重复头，把它们放在末尾能保证 content-length 不被同名覆盖，也保证 401 的挑战头一定发出去。
    headers.extend(
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in rejection.headers.items()
    )
    await send(
        {
            "type": "http.response.start",
            "status": rejection.status_code,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
