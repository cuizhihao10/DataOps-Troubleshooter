"""验证 MCP 服务端 Fixture 仓储的确定性查找和错误标准化。

成功场景必须返回指定工具/资源的固定观察；未知 scenario_id 与缺失结果分别映射到
INVALID_REQUEST 和 EMPTY_RESULT，不能把内部 KeyError 泄露给客户端。
"""

from pathlib import Path

from app.core.fixture_registry import FixtureRegistry
from app.domain.tooling import McpToolRequest, ToolErrorCode, ToolName
from mcp_server.repository import FixtureToolRepository


def _request(scenario_id: str, resource_id: str) -> McpToolRequest:
    return McpToolRequest.model_validate(
        {
            "resource_id": resource_id,
            "time_range": {
                "start": "2026-07-10T00:00:00+08:00",
                "end": "2026-07-10T03:00:00+08:00",
            },
            "scenario_id": scenario_id,
            "trace_id": "trace_repository_001",
        }
    )


def test_repository_returns_scenario_driven_success() -> None:
    repository = FixtureToolRepository(
        FixtureRegistry.from_directory(Path("data/fixtures/scenarios"))
    )
    response = repository.execute(
        ToolName.LTS_GET_TASK_STATUS,
        _request("cross_chain_pk_conflict", "dws_order_report_daily"),
    )

    assert response.ok is True
    assert response.data["status"] == "failed"
    assert response.evidence[0].source_id == "lts_status_dws_order_report_daily"


def test_repository_standardizes_unknown_scenario() -> None:
    repository = FixtureToolRepository(
        FixtureRegistry.from_directory(Path("data/fixtures/scenarios"))
    )
    response = repository.execute(
        ToolName.LTS_GET_TASK_STATUS,
        _request("unknown_scenario", "dws_order_report_daily"),
    )

    assert response.ok is False
    assert response.error_code is ToolErrorCode.INVALID_REQUEST


def test_repository_standardizes_missing_tool_result() -> None:
    repository = FixtureToolRepository(
        FixtureRegistry.from_directory(Path("data/fixtures/scenarios"))
    )
    response = repository.execute(
        ToolName.LTS_GET_TASK_STATUS,
        _request("cross_chain_pk_conflict", "unknown_task"),
    )

    assert response.ok is False
    assert response.error_code is ToolErrorCode.EMPTY_RESULT
