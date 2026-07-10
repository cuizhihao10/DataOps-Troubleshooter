import httpx
import pytest

from app.api.main import app


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
    assert payload["mcp_tools_available"] == [
        "lts.get_dependency_topology",
        "lts.get_task_log",
        "lts.get_task_status",
    ]
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
