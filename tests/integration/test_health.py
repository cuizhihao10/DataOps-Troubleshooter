"""验证 FastAPI 启动审计和健康响应的集成测试。

无数据库的快速测试模式仍会真实启动 MCP 子进程并发现九个工具，同时明确报告数据库被
禁用。Docker 模式的数据库健康与知识计数由 PostgreSQL 专用测试和容器验证覆盖。
"""

import httpx
import pytest

from app.api.main import app
from app.domain.tooling import ToolName


@pytest.mark.asyncio
async def test_health_reports_validated_contract_baseline() -> None:
    """验证 FastAPI lifespan 完成真实依赖审计后才返回稳定健康契约。

    测试显式进入 lifespan，因此会加载 Fixture/Golden Case/Prompt 并跨 stdio 发现九个 MCP 工具；
    ASGITransport 随后在不开放网络端口的情况下调用 `/health`。断言同时覆盖资产数量、工具白名单、
    无数据库模式和预算版本，防止路由只返回固定 `ok` 而没有反映实际初始化状态。
    """

    # 手动进入 lifespan 才能测试启动审计；只调用路由会绕过真实依赖初始化。
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/health")

    # HTTP 成功后继续逐字段检查，避免一个空的 200 响应被误判为系统就绪。
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["fixtures_loaded"] == 5
    assert payload["golden_cases_loaded"] == 5
    assert payload["mcp_tools_available"] == sorted(tool.value for tool in ToolName)
    assert payload["database_status"] == "disabled"
    assert payload["knowledge_nodes_loaded"] == 0
    assert payload["knowledge_edges_loaded"] == 0
    assert payload["contracts"] == {
        "planner_prompt": "planner-react:v1",
        "mcp": "mcp-tools:v1",
        "golden_case": "golden-case:v1",
    }
    assert payload["limits"] == {
        "max_react_steps": 6,
        "max_graph_hops": 2,
        "max_audit_revisions": 1,
        "tool_retry_count": 1,
    }
