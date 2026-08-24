"""验证鉴权与限流真的挂在请求路径上，而不只是存在一个正确的判定类。

测试用环境变量把实例切换到 bearer 模式后进入 lifespan，因此覆盖的是"启动审计 + 中间件 + 路由"
这条完整链路。断言重点有三条：受保护前缀缺令牌必须 401；`/health` 与 `/demo` 必须保持公开，否则
容器探针和静态页面都会被自己的鉴权挡住；配额耗尽必须返回 429 且带 `Retry-After`。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from app.api.main import app
from app.core.settings import get_settings

# 32 字符是守卫要求的最小长度；测试令牌是显式的合成字符串，不来自任何真实部署。
TEST_TOKEN = "dataops-test-token-0123456789abcd"


@pytest.fixture
def bearer_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把进程切换到 bearer 鉴权与极小配额，并在退出时清理 Settings 缓存。

    `get_settings` 是进程级 lru_cache，若不在前后各清一次，本测试的环境覆盖会泄漏给同一会话的
    其它测试文件，制造"单独跑绿、整体跑红"的不可复现失败。配额压到 3 次是为了在不 sleep 的前提下
    触发 429。
    """

    monkeypatch.setenv("DATAOPS_API_AUTH_MODE", "bearer")
    monkeypatch.setenv("DATAOPS_API_AUTH_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATAOPS_API_RATE_LIMIT_REQUESTS", "3")
    monkeypatch.setenv("DATAOPS_API_RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_protected_prefixes_require_bearer_token_while_probes_stay_public(
    bearer_environment: None,
) -> None:
    """验证 `/api/v1` 与 `/metrics` 需要令牌，`/health` 与 `/demo` 仍然公开。

    带上正确令牌后不应再返回 401；本地无数据库时诊断 runtime 未装配，因此期望 503 而不是 200——
    这恰好证明请求已经穿过鉴权进入路由，而不是被中间件提前挡下。健康接口同时公开鉴权模式，让
    运维无需试探接口就能确认实例是否需要凭据。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            demo = await client.get("/demo")
            anonymous = await client.post("/api/v1/sessions", json={"title": "无令牌"})
            metrics = await client.get("/metrics")
            authorized = await client.post(
                "/api/v1/sessions",
                json={"title": "带令牌"},
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            )

    assert health.status_code == 200
    assert health.json()["security"]["mode"] == "bearer"
    assert health.json()["security"]["rate_limit_requests"] == 3
    assert demo.status_code == 200
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == 'Bearer realm="dataops-api"'
    assert anonymous.json()["detail"]["error_code"] == "unauthorized"
    # 响应体不得包含令牌、其摘要或"是否已配置令牌"的任何线索。
    assert TEST_TOKEN not in anonymous.text
    assert metrics.status_code == 401
    assert authorized.status_code == 503


@pytest.mark.asyncio
async def test_exhausted_quota_returns_429_with_retry_after(bearer_environment: None) -> None:
    """验证同一来源打满配额后返回 429 并给出整数 `Retry-After`。

    错误令牌同样消耗配额，因此该测试也固定了"限流先于鉴权"的顺序：第四次请求必须是 429 而不是
    第四个 401。`Retry-After` 必须是可解析的正整数秒，否则客户端只能靠猜来退避。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            statuses = [
                (await client.get("/api/v1/runs/run-does-not-exist")).status_code
                for _ in range(4)
            ]
            throttled = await client.get("/api/v1/runs/run-does-not-exist")

    assert statuses == [401, 401, 401, 429]
    assert throttled.status_code == 429
    assert throttled.json()["detail"]["error_code"] == "rate_limited"
    assert int(throttled.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_default_deployment_keeps_api_open_but_reports_rate_limit_quota() -> None:
    """验证默认（未配置令牌）部署仍可直接调用 API，且健康接口如实报告限流配额。

    默认打开是刻意的产品选择：求职演示需要 `docker compose up` 后立刻可用。但"无需令牌"不等于
    "无限调用"，因此配额必须在 disabled 模式下依然公开且生效，避免把成本保护说成可选项。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            anonymous = await client.post("/api/v1/sessions", json={"title": "默认部署"})

    assert health.json()["security"]["mode"] == "disabled"
    assert health.json()["security"]["rate_limit_requests"] == 120
    assert health.json()["security"]["protected_path_prefixes"] == ["/api/v1", "/metrics"]
    # 没有数据库时返回 503 而不是 401，说明默认部署没有被鉴权挡住。
    assert anonymous.status_code == 503
