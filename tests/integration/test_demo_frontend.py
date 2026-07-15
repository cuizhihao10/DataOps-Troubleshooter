"""验证单页 Demo 的静态托管、同源资源和路径安全边界。

测试不启动真实模型或 PostgreSQL；它只通过 FastAPI lifespan 读取打包后的静态资产，证明前端
可以在 Docker 中加载并且不会把任意本地路径暴露给浏览器。诊断 API 的异步状态机由其它集成
测试覆盖，本文件专注于前端入口和安全资源分发契约。
"""

from __future__ import annotations

import httpx
import pytest

from app.api.main import app


@pytest.mark.asyncio
async def test_demo_page_serves_static_assets_and_documents_safe_async_flow() -> None:
    """验证 `/demo`、CSS 和模块 JavaScript 都能通过同源路由读取。

    断言关键状态词和 `textContent` 安全写入存在，避免页面退化成只展示同步 completed 的假
    交互；同时检查 HTML 明确禁止 Thought，保证学习演示不把隐藏思维链当作产品数据展示。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            page = await client.get("/demo")
            styles = await client.get("/demo/static/styles.css")
            script = await client.get("/demo/static/app.js")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "/demo/static/app.js" in page.text
    assert "queued/running" in page.text
    assert "Thought" in page.text
    assert styles.status_code == 200
    assert ".workspace-grid" in styles.text
    assert script.status_code == 200
    assert "async function pollRun" in script.text
    assert "textContent" in script.text
    assert "error.status === 409" in script.text
    assert "/api/v1/memories/" in script.text
    assert 'body: JSON.stringify({ decision })' in script.text
    assert "confirm-memory" in page.text
    assert "reject-memory" in page.text
    assert "cancel-run" in page.text
    assert "resume-run" in page.text
    assert "delete-memory" in page.text
    assert "/api/v1/runs/" in script.text and "/cancel" in script.text and "/resume" in script.text
    assert "DELETE" in script.text


@pytest.mark.asyncio
async def test_demo_asset_route_rejects_path_traversal_and_unknown_files() -> None:
    """验证静态资源路由只允许 demo 目录内的普通文件。

    `../` 和不存在的资源都应返回 404；路由不能把仓库中的 `.env`、Python 源码或其它配置
    作为静态响应。该检查与 API 返回 404 的语义不同，专门覆盖文件系统边界。
    """

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            traversal = await client.get("/demo/static/../../.env")
            missing = await client.get("/demo/static/not-found.txt")

    assert traversal.status_code == 404
    assert missing.status_code == 404
