"""验证 MCP 传输选型的默认值、fail-closed 配置校验与两条传输的工厂接线（`mcp-transport:v1`）。

传输是部署配置，因此它最可能出错的地方不在协议实现里，而在"配置组合被接受了但语义是错的"：
HTTP 却没有令牌（匿名工具端点）、stdio 却配了令牌（部署者以为端点受保护）、网关 URL 里内嵌
凭据（凭据随异常文本与 trace 外泄）。这些必须在进程开始监听之前失败，所以断言落在 Settings 与
工厂两层，而不是等一次真实请求。

本文件不发起任何网络或子进程活动：两个客户端的构造都无副作用，真实握手只发生在启动阶段的工具
发现。跨真实网关的协议、鉴权与失败分类断言在 `tests/integration/test_mcp_streamable_http.py`。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import Settings
from app.mcp.client import StdioMcpClient
from app.mcp.factory import create_mcp_client
from app.mcp.protocol import MCP_TRANSPORT_CONTRACT_ID
from app.mcp.streamable_http import StreamableHttpMcpClient
from mcp_server.security import build_gateway_guard

GATEWAY_TOKEN = "unit-gateway-token-0123456789abcdef"


def test_default_settings_select_stdio_without_token_and_pin_the_contract_id() -> None:
    """验证零配置环境选中 stdio、不要求令牌，且传输契约 ID 与模块常量一致。

    默认值刻意不是生产形态：`tests/conftest.py` 会清空所有 `DATAOPS_*`，若默认是 HTTP，任何应用
    启动（健康检查、/demo、`--skip-postgres` 离线评测）都要先有一个可达网关。契约 ID 在这里一并
    断言，因为 lifespan 逐项比对时不一致就拒绝启动，值写错会表现成一条与传输无关的启动失败。
    """

    settings = Settings(_env_file=None)

    assert settings.mcp_transport == "stdio"
    assert settings.mcp_auth_token is None
    assert settings.mcp_transport_contract_id == MCP_TRANSPORT_CONTRACT_ID
    assert isinstance(create_mcp_client(settings), StdioMcpClient)


def test_transport_and_token_must_agree_in_both_directions() -> None:
    """验证 HTTP 缺令牌与 stdio 配令牌两种组合都在 Settings 边界被拒绝。

    两个方向都要拦，理由不同却同样重要：HTTP 缺令牌会把九个只读工具变成匿名可访问的全链路证据
    端点；stdio 配了令牌说明部署者以为端点受保护，而 stdio 根本没有可鉴权的网络面，这种误解只有
    在启动期拒绝才能被纠正——运行期没有任何症状可供发现。
    """

    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_transport="streamable-http")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_auth_token=SecretStr(GATEWAY_TOKEN))


def test_gateway_url_must_not_embed_credentials() -> None:
    """验证网关地址内嵌 userinfo 时拒绝启动，与 chat_base_url 同一条规则。

    URL 里的凭据会随异常文本、日志和 trace 一起外泄，而 URL 恰恰是这些出口默认会打印的字段。
    令牌只允许走 `Authorization` 头，因此这条检查是"令牌绝不进 URL"的配置侧对应物。
    """

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            mcp_transport="streamable-http",
            mcp_auth_token=SecretStr(GATEWAY_TOKEN),
            mcp_http_url="http://user:password@gateway.test:8900/mcp",
        )


@pytest.mark.asyncio
async def test_streamable_http_settings_build_a_pooled_client_without_leaking_the_token() -> None:
    """验证 HTTP 配置构造出指向配置地址的池化客户端，且令牌不出现在 repr 中。

    工厂返回的必须是 HTTP 实现而不是 stdio：静默回退到子进程是一种不会报错的部署漂移，容器里
    "以为在打网关、其实还在起子进程"只能靠这条断言和 `/health` 的 mcp 小节发现。同时断言超时来自
    统一的工具超时预算，换传输不会悄悄改变 Action 的等待上限；构造期不发请求，因此没有网络副作用。
    """

    settings = Settings(
        _env_file=None,
        mcp_transport="streamable-http",
        mcp_auth_token=SecretStr(GATEWAY_TOKEN),
        mcp_http_url="http://gateway.test:8900/mcp",
        tool_timeout_seconds=7,
    )

    client = create_mcp_client(settings)

    assert isinstance(client, StreamableHttpMcpClient)
    assert GATEWAY_TOKEN not in repr(settings)
    try:
        assert client._url == "http://gateway.test:8900/mcp"
        assert client._timeout_seconds == 7
        assert client._http_client.headers["Authorization"] == f"Bearer {GATEWAY_TOKEN}"
        # 直连是安全要求：跟随重定向会把 Bearer 令牌送去未打算信任的主机，读系统代理则会把
        # "端口无人监听"变成代理侧读超时，从而让 SERVICE_UNAVAILABLE 被误分类成 TIMEOUT。
        assert client._http_client.follow_redirects is False
        assert client._http_client.trust_env is False
    finally:
        await client.aclose()


def test_header_injectable_tokens_are_rejected_at_construction() -> None:
    """验证含 CRLF 或空白的令牌在客户端构造期即被拒绝，而不是等第一次调用。

    `\\r\\n` 会变成请求头注入；httpx 只在真正发请求时才报错，那时进程已经通过启动审计并对外宣称
    健康。把校验放在构造期，等于让"令牌写坏了"这件事只能表现成启动失败这一种形态。
    """

    for invalid in ("bad\r\ntoken-0123456789abcdefghijklmn", "has space 0123456789abcdefghij"):
        with pytest.raises(ValueError):
            StreamableHttpMcpClient(
                url="http://gateway.test:8900/mcp",
                auth_token=SecretStr(invalid),
            )


def test_gateway_guard_is_bearer_only_and_refuses_to_build_without_a_token() -> None:
    """验证网关守卫复用配置配额、模式恒为 bearer，且缺令牌时拒绝构造。

    守卫没有 disabled 档：资源 API 那一档存在的理由是"本地演示要开箱可用"，而网关端口一旦被监听
    就没有这种低风险语境。缺令牌抛错是对 Settings 校验的有意冗余——本函数也可能被未来的独立入口
    调用，而一个"忘了配令牌就裸奔"的网关是不可接受的失败模式。
    """

    settings = Settings(
        _env_file=None,
        mcp_transport="streamable-http",
        mcp_auth_token=SecretStr(GATEWAY_TOKEN),
        mcp_rate_limit_requests=600,
        mcp_rate_limit_window_seconds=60,
    )

    guard = build_gateway_guard(settings)

    assert guard.mode == "bearer"
    assert guard.limiter.max_requests == 600
    assert guard.limiter.window_seconds == 60
    with pytest.raises(ValueError):
        build_gateway_guard(Settings(_env_file=None))
