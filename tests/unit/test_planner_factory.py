"""验证 Planner Provider 的 disabled 默认值、SecretStr 边界与运行时工厂。

配置测试不发送网络请求，只确认无 key 环境可以启动，启用 Provider 时必须提供密钥，并且健康/
对象 repr 不泄露明文。注入 SDK 客户端用于验证工厂接线而不创建额外连接池。
"""

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agents.factory import create_planner_runtime
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
