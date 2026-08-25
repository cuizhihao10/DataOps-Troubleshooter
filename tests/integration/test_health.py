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
    assert payload["fixtures_loaded"] == 18
    assert payload["golden_cases_loaded"] == 28
    assert payload["mcp_tools_available"] == sorted(tool.value for tool in ToolName)
    assert payload["capabilities_available"] == [name.value for name in CapabilityName]
    assert payload["database_status"] == "disabled"
    assert payload["knowledge_nodes_loaded"] == 0
    assert payload["knowledge_edges_loaded"] == 0
    assert payload["knowledge_nodes_embedded"] == 0
    assert payload["documents_loaded"] == 0
    assert payload["document_chunks_loaded"] == 0
    assert payload["document_chunks_embedded"] == 0
    assert payload["contracts"] == {
        "planner_prompt": "planner-react:v8",
        "planner_provider": "openai-compatible-planner:v1",
        "auditor_prompt": "auditor-report:v2",
        "auditor_provider": "openai-compatible-auditor:v1",
        "mcp": "mcp-tools:v1",
        "golden_case": "golden-case:v7",
        "runtime_capabilities": "runtime-capabilities:v1",
        "react_loop": "langgraph-react-loop:v3",
        "audited_report_workflow": "audited-report-workflow:v2",
        "diagnosis_workflow": "audited-diagnosis-workflow:v2",
        "diagnosis_api": "diagnosis-resources:v4",
        "session_checkpoint": "session-checkpoint:v1",
        "case_memory": "case-memory:v2",
        "graph_retrieval": "graphrag-retrieval:v3",
        "graph_evidence_bundle": "graphrag-evidence-bundle:v2",
        "document_retrieval": "document-retrieval:v1",
        "run_trace": "run-trace:v1",
        "api_auth": "api-auth:v1",
        "run_stream": "run-stream:v1",
        "model_transient_retry": "model-transient-retry:v1",
    }
    assert payload["limits"] == {
        # 步数预算严格大于 Golden 集里最长的必需工具集（6 个），给真实模型留出试探余量；墙钟预算
        # 随之放宽到 240s，因为实测 Planner 单次 8–15s，8 步最坏要四次决策加工具与检索时间，并且
        # 还要能容纳一次瞬时重试的最坏开销（一次超时 30s 加 1s 退避）。
        "max_react_steps": 8,
        "max_parallel_tool_actions": 3,
        "react_total_timeout_seconds": 240.0,
        "max_graph_hops": 2,
        "max_audit_revisions": 1,
        "tool_retry_count": 1,
        "chat_transient_retry_attempts": 2,
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
        # Auditor 超时比 Planner 宽：审计要读完整草稿并逐条核对引用，实测耗时是 Planner 的两到
        # 三倍，共用 30s 会让审计频繁超时并按"审计不可用"直接降级。健康检查逐字暴露这个差异，
        # 部署者因此能在不读代码的情况下确认两个角色不是同一个旋钮。
        "timeout_seconds": 90.0,
        "schema_repair_count": 1,
    }
    assert payload["memory"] == {
        "status": "disabled",
        "contract_id": "case-memory:v2",
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
        "contract_id": "diagnosis-resources:v4",
        "checkpoint_contract_id": "session-checkpoint:v1",
        "execution_mode": "postgres-worker",
        "worker_status": "disabled",
        "worker_poll_seconds": 0.25,
        "worker_lease_seconds": 180.0,
        "worker_heartbeat_seconds": 30.0,
        "worker_max_attempts": 2,
        "retrieval_seed_limit": 5,
    }
    assert payload["retrieval"] == {
        "embedding_provider": "deterministic-hash:v1",
        "embedding_dimensions": 128,
        "embedding_model": "BAAI/bge-m3",
        "embedding_endpoint_host": "api.siliconflow.cn",
        "rerank_provider": "disabled",
        "rerank_model": "BAAI/bge-reranker-v2-m3",
        "rerank_endpoint_host": "api.siliconflow.cn",
        "rerank_candidate_multiplier": 3,
        "rerank_blend_weight": 0.4,
        "score_weights": {
            "semantic": 0.45,
            "lexical": 0.1,
            "path": 0.25,
            "reliability": 0.1,
            "freshness": 0.1,
        },
        "document_score_weights": {
            "semantic": 0.6,
            "lexical": 0.25,
            "authority": 0.15,
        },
        "document_chunk_limit": 4,
        "evidence_budget": {
            "max_bytes": 6000,
            "max_nodes": 8,
            "max_paths": 4,
            "max_documents": 3,
        },
    }
    # 默认部署必须显式声明"鉴权关闭"，否则演示者无法区分"这个实例不需要令牌"和"健康接口忘了报"。
    assert payload["security"] == {
        "mode": "disabled",
        "contract_id": "api-auth:v1",
        "protected_path_prefixes": ["/api/v1", "/metrics"],
        "rate_limit_requests": 120,
        "rate_limit_window_seconds": 60.0,
    }
    # 推流预算与"鉴权模式下是否可用"一起公开：浏览器 EventSource 不能带 Authorization 头，因此
    # available_under_auth 必须随鉴权模式变化，而不是硬编码为 true。
    assert payload["stream"] == {
        "contract_id": "run-stream:v1",
        "poll_seconds": 0.5,
        "keepalive_seconds": 15.0,
        "max_seconds": 300.0,
        "available_under_auth": True,
    }
