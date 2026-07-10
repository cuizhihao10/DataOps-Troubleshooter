import pytest

from app.domain.planner import ToolAction
from app.domain.tooling import ToolErrorCode
from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor


def _action(
    scenario_id: str,
    resource_id: str,
    trace_id: str,
    *,
    tool_name: str = "lts.get_task_status",
) -> ToolAction:
    return ToolAction.model_validate(
        {
            "tool_name": tool_name,
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

    assert await client.list_tools() == (
        "lts.get_dependency_topology",
        "lts.get_task_log",
        "lts.get_task_status",
    )
    descriptors = await client.list_tool_descriptors()
    assert len(descriptors) == 3
    assert all(descriptor.read_only for descriptor in descriptors)
    assert all(not descriptor.destructive for descriptor in descriptors)
    assert all(descriptor.idempotent for descriptor in descriptors)
    assert all(descriptor.has_output_schema for descriptor in descriptors)


@pytest.mark.asyncio
async def test_action_crosses_mcp_protocol_and_becomes_observation() -> None:
    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
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
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "expected_data_key"),
    [
        ("lts.get_task_log", "component_error_code"),
        ("lts.get_dependency_topology", "upstream_task"),
    ],
)
async def test_remaining_lts_tools_cross_real_mcp_protocol(
    tool_name: str,
    expected_data_key: str,
) -> None:
    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "cross_chain_pk_conflict",
            "dws_order_report_daily",
            f"trace_{tool_name.replace('.', '_')}_001",
            tool_name=tool_name,
        )
    )

    assert observation.response.ok is True
    assert expected_data_key in observation.response.data
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
async def test_mcp_failure_response_is_preserved_without_fake_evidence() -> None:
    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
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
    assert len(observation.tool_events) == 1


@pytest.mark.asyncio
async def test_transient_mcp_failure_retries_once_and_preserves_both_events() -> None:
    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "lts_empty_result",
            "lts_inventory_snapshot_daily",
            "trace_protocol_timeout_001",
            tool_name="lts.get_dependency_topology",
        )
    )

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.TIMEOUT
    assert observation.evidence == []
    assert [event.attempt for event in observation.tool_events] == [1, 2]
    assert all(event.retryable for event in observation.tool_events)
    assert observation.tool_events[0].event_id != observation.tool_events[1].event_id
