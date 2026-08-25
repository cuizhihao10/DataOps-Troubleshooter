"""验证 Planner/Auditor Provider 的 disabled 默认值、SecretStr 边界与运行时工厂。

配置测试不发送网络请求，只确认无 key 环境可以启动，启用两个角色时必须提供密钥，并且对象
repr 不泄露明文。注入 SDK 客户端用于验证两个工厂接线和资源所有权。
"""

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agents.factory import create_auditor_runtime, create_planner_runtime
from app.agents.retrying import (
    RetryingAuditorChatProvider,
    RetryingPlannerChatProvider,
)
from app.core.settings import Settings


def test_default_settings_keep_paid_planner_provider_disabled() -> None:
    """验证干净环境默认不要求 API key，工厂明确返回 None。

    该默认值保证单测和 Docker 演示可离线启动；模型名和端点仍可公开说明预期配置，但不会因为
    存在默认模型字符串而创建客户端或发送探测请求。
    """

    settings = Settings(_env_file=None)

    assert settings.chat_provider == "disabled"
    assert settings.chat_api_key is None
    assert create_planner_runtime(settings) is None
    assert create_auditor_runtime(settings) is None


def test_enabled_provider_requires_secret_key_and_rejects_url_credentials() -> None:
    """验证启用模型时缺 key 或把凭据塞进 base_url 都在 Settings 边界失败。

    两个错误都应产生 Pydantic ValidationError，防止认证信息进入普通 URL、健康响应或日志；
    调用者必须通过 DATAOPS_CHAT_API_KEY 的 SecretStr 路径提供本地密钥。
    """

    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_provider="openai-compatible")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            chat_base_url="https://user:password@example.test/v1",
        )


@pytest.mark.asyncio
async def test_factory_builds_runtime_without_exposing_secret_or_owning_injected_client() -> None:
    """验证启用配置可构造 Agent/Provider，SecretStr repr 被遮蔽且注入客户端由测试关闭。

    工厂不发请求；MockTransport handler 若被调用会失败。runtime.aclose 不关闭外部客户端，随后
    测试显式关闭，证明资源所有权与 FastAPI lifespan 约定一致。
    """

    async def unexpected_request(request: httpx.Request) -> httpx.Response:
        """在工厂测试中拒绝任何意外 HTTP 请求，证明构造阶段没有模型探测。

        参数仅用于满足 MockTransport 签名；若函数被调用立即抛出 AssertionError，不返回伪响应。
        """

        raise AssertionError(f"unexpected Planner request to {request.url.host}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request))
    sdk_client = AsyncOpenAI(
        api_key="local_test_secret",
        base_url="https://example.test/v1",
        http_client=http_client,
        max_retries=0,
    )
    settings = Settings(
        _env_file=None,
        chat_provider="openai-compatible",
        chat_base_url="https://example.test/v1",
        chat_api_key="local_test_secret",
    )

    runtime = create_planner_runtime(settings, client=sdk_client)

    assert runtime is not None
    assert "local_test_secret" not in repr(settings)
    await runtime.aclose()
    assert not http_client.is_closed
    await sdk_client.close()


@pytest.mark.asyncio
async def test_auditor_factory_builds_independent_runtime_without_network_probe() -> None:
    """验证启用配置可构造独立 Auditor，且关闭 runtime 不关闭注入客户端。

    MockTransport 若收到请求会失败，证明工厂只加载/审计 Prompt 与构造对象；随后 runtime.aclose
    不关闭外部连接池，测试最后显式释放，明确资源所有权。
    """

    async def unexpected_request(request: httpx.Request) -> httpx.Response:
        """拒绝任何构造期 HTTP 请求，防止健康启动产生付费模型探测。

        request 只用于错误定位；函数总是抛 AssertionError，不返回合成模型结果。
        """

        raise AssertionError(f"unexpected Auditor request to {request.url.host}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request))
    sdk_client = AsyncOpenAI(
        api_key="local_test_secret",
        base_url="https://example.test/v1",
        http_client=http_client,
        max_retries=0,
    )
    settings = Settings(
        _env_file=None,
        chat_provider="openai-compatible",
        chat_base_url="https://example.test/v1",
        chat_api_key="local_test_secret",
    )

    runtime = create_auditor_runtime(settings, client=sdk_client)

    assert runtime is not None
    await runtime.aclose()
    assert not http_client.is_closed
    await sdk_client.close()


@pytest.mark.asyncio
async def test_planner_and_auditor_receive_independent_request_timeouts() -> None:
    """验证两个角色的超时来自不同配置项，Auditor 更宽而 Planner 保持紧。

    首次真实模型评测实测 Planner 单次 8–15s、Auditor 单次 22–30s：审计要读完整草稿并逐条核对
    引用，输出也接近 1100 token。共用一个 30s 旋钮时四次审计有三次超时，而超时按"审计不可用"
    直接降级且不消耗返工预算，于是三个案例全部拿不到 accepted 报告。Planner 不能跟着放宽，因为
    它跑在 react_total_timeout_seconds 预算内、一次挂死就吃掉整轮；Auditor 不在该预算内。

    这里不注入客户端，因为超时是在工厂构造 AsyncOpenAI 时落到客户端上的；构造期不发请求，所以
    断言不产生付费调用。
    """

    settings = Settings(
        _env_file=None,
        chat_provider="openai-compatible",
        chat_base_url="https://example.test/v1",
        chat_api_key="local_test_secret",
    )
    assert settings.chat_timeout_seconds == 30.0
    assert settings.auditor_timeout_seconds == 90.0

    planner = create_planner_runtime(settings)
    auditor = create_auditor_runtime(settings)
    assert planner is not None
    assert auditor is not None
    try:
        assert planner.provider._client.timeout == settings.chat_timeout_seconds
        assert auditor.provider._client.timeout == settings.auditor_timeout_seconds
    finally:
        await planner.aclose()
        await auditor.aclose()


@pytest.mark.asyncio
async def test_factory_wraps_both_agents_in_transient_retry_but_keeps_pool_ownership() -> None:
    """验证工厂给两个 Agent 都接上重试包装器，而 runtime 仍持有具体 Provider 以便关闭连接池。

    这条接线是重试能生效的唯一途径：Agent 只依赖协议，若工厂直接把具体 Provider 交给它，
    一次瞬时 429 就会立刻变成 planner_provider_error 终态。同时断言 runtime.provider 不是包装器，
    因为包装层不持有 HTTP 资源，aclose 责任必须留在真正创建了 AsyncOpenAI 的那一层。
    """

    settings = Settings(
        _env_file=None,
        chat_provider="openai-compatible",
        chat_base_url="https://example.test/v1",
        chat_api_key="local_test_secret",
    )

    planner = create_planner_runtime(settings)
    auditor = create_auditor_runtime(settings)
    assert planner is not None
    assert auditor is not None
    try:
        assert isinstance(planner.agent._provider, RetryingPlannerChatProvider)
        assert isinstance(auditor.agent._provider, RetryingAuditorChatProvider)
        assert planner.agent._provider._inner is planner.provider
        assert auditor.agent._provider._inner is auditor.provider
        assert planner.agent._provider._policy == settings.transient_retry_policy()
    finally:
        await planner.aclose()
        await auditor.aclose()

