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
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/health")

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
