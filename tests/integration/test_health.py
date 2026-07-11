"""验证 FastAPI 启动审计和健康响应的集成测试。

无数据库的快速测试模式仍会真实启动 MCP 子进程并发现九个工具，同时明确报告数据库被
禁用。Docker 模式的数据库健康与知识计数由 PostgreSQL 专用测试和容器验证覆盖。
"""

import httpx
import pytest

from app.api.main import app
from app.capabilities import CapabilityName
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
    assert payload["capabilities_available"] == [name.value for name in CapabilityName]
    assert payload["database_status"] == "disabled"
    assert payload["knowledge_nodes_loaded"] == 0
    assert payload["knowledge_edges_loaded"] == 0
    assert payload["knowledge_nodes_embedded"] == 0
    assert payload["contracts"] == {
        "planner_prompt": "planner-react:v4",
        "planner_provider": "openai-compatible-planner:v1",
        "auditor_prompt": "auditor-report:v2",
        "auditor_provider": "openai-compatible-auditor:v1",
        "mcp": "mcp-tools:v1",
        "golden_case": "golden-case:v1",
        "runtime_capabilities": "runtime-capabilities:v1",
        "react_loop": "langgraph-react-loop:v2",
        "audited_report_workflow": "audited-report-workflow:v2",
        "diagnosis_workflow": "audited-diagnosis-workflow:v2",
        "diagnosis_api": "diagnosis-resources:v2",
        "session_checkpoint": "session-checkpoint:v1",
        "case_memory": "case-memory:v1",
        "graph_retrieval": "graphrag-retrieval:v2",
        "graph_evidence_bundle": "graphrag-evidence-bundle:v1",
    }
    assert payload["limits"] == {
        "max_react_steps": 6,
        "react_total_timeout_seconds": 60.0,
        "max_graph_hops": 2,
        "max_audit_revisions": 1,
        "tool_retry_count": 1,
    }
    assert payload["planner"] == {
        "status": "disabled",
        "provider": "disabled",
        "model": "gpt-5.6",
        "endpoint_host": "api.openai.com",
        "timeout_seconds": 30.0,
        "schema_repair_count": 1,
    }
    assert payload["auditor"] == {
        "status": "disabled",
        "provider": "disabled",
        "model": "gpt-5.6",
        "endpoint_host": "api.openai.com",
        "timeout_seconds": 30.0,
        "schema_repair_count": 1,
    }
    assert payload["memory"] == {
        "status": "disabled",
        "contract_id": "case-memory:v1",
        "embedding_provider": "deterministic-hash:v1",
        "embedding_dimensions": 128,
        "dedup_similarity_threshold": 0.92,
        "graph_similarity_threshold": 0.75,
        "default_search_limit": 5,
        "query_max_chars": 4000,
        "counts": {"pending": 0, "confirmed": 0, "rejected": 0},
    }
    assert payload["diagnosis_api"] == {
        "status": "disabled",
        "contract_id": "diagnosis-resources:v2",
        "checkpoint_contract_id": "session-checkpoint:v1",
        "execution_mode": "synchronous",
        "retrieval_seed_limit": 5,
    }
    assert payload["retrieval"] == {
        "embedding_provider": "deterministic-hash:v1",
        "embedding_dimensions": 128,
        "score_weights": {
            "semantic": 0.45,
            "lexical": 0.1,
            "path": 0.25,
            "reliability": 0.1,
            "freshness": 0.1,
        },
        "evidence_budget": {
            "max_bytes": 6000,
            "max_nodes": 8,
            "max_paths": 4,
        },
    }
