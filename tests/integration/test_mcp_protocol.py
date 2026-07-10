import pytest

from app.domain.planner import ToolAction
from app.domain.tooling import ToolErrorCode
from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor


def _action(scenario_id: str, resource_id: str, trace_id: str) -> ToolAction:
    return ToolAction.model_validate(
        {
            "tool_name": "lts.get_task_status",
            "arguments": {
                "resource_id": resource_id,
                "time_range": {
                    "start": "2026-07-10T00:00:00+08:00",
                    "end": "2026-07-10T03:00:00+08:00",
                },
                "scenario_id": scenario_id,
                "trace_id": trace_id,
            },
        }
    )


@pytest.mark.asyncio
async def test_real_mcp_protocol_lists_read_only_lts_tool() -> None:
    client = StdioMcpClient()

    assert await client.list_tools() == ("lts.get_task_status",)
    descriptors = await client.list_tool_descriptors()
    assert len(descriptors) == 1
    assert descriptors[0].read_only is True
    assert descriptors[0].destructive is False
    assert descriptors[0].idempotent is True
    assert descriptors[0].has_output_schema is True


@pytest.mark.asyncio
async def test_action_crosses_mcp_protocol_and_becomes_observation() -> None:
    executor = McpToolExecutor(StdioMcpClient())
    observation = await executor.execute(
        _action(
            "cross_chain_pk_conflict",
            "dws_order_report_daily",
            "trace_protocol_success_001",
        )
    )

    assert observation.response.ok is True
    assert observation.response.data["status"] == "failed"
    assert observation.tool_event.tool_name.value == "lts.get_task_status"
    assert observation.tool_event.trace_id == "trace_protocol_success_001"
    assert observation.observation_refs


@pytest.mark.asyncio
async def test_mcp_failure_response_is_preserved_without_fake_evidence() -> None:
    executor = McpToolExecutor(StdioMcpClient())
    observation = await executor.execute(
        _action(
            "lts_empty_result",
            "lts_inventory_snapshot_daily",
            "trace_protocol_empty_001",
        )
    )

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.EMPTY_RESULT
    assert observation.evidence == []
    assert observation.observation_refs == []
    assert observation.tool_event.retryable is False
