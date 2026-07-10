from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.tooling import (
    RETRYABLE_TOOL_ERRORS,
    McpToolRequest,
    McpToolResponse,
    TimeRange,
    ToolErrorCode,
    ToolName,
)

EXPECTED_TOOL_NAMES = {
    "lts.get_task_status",
    "lts.get_task_log",
    "lts.get_dependency_topology",
    "bds.get_task_status",
    "bds.get_task_log",
    "bds.get_table_info",
    "flashsync.get_sync_delay",
    "flashsync.get_sync_log",
    "flashsync.check_consistency",
}


def test_tool_allowlist_matches_product_contract() -> None:
    assert {tool.value for tool in ToolName} == EXPECTED_TOOL_NAMES


def test_time_range_requires_timezone_and_increasing_values() -> None:
    with pytest.raises(ValidationError):
        TimeRange(
            start=datetime(2026, 7, 10, 1, 0),
            end=datetime(2026, 7, 10, 2, 0),
        )

    with pytest.raises(ValidationError):
        TimeRange(
            start=datetime(2026, 7, 10, 2, 0, tzinfo=UTC),
            end=datetime(2026, 7, 10, 1, 0, tzinfo=UTC),
        )


def test_unknown_tool_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        ToolName("lts.fetch_everything")


def test_tool_request_rejects_blank_resource_id() -> None:
    with pytest.raises(ValidationError):
        McpToolRequest.model_validate(
            {
                "resource_id": "",
                "time_range": {
                    "start": "2026-07-10T01:00:00+08:00",
                    "end": "2026-07-10T02:00:00+08:00",
                },
                "scenario_id": "valid_scenario",
                "trace_id": "trace_001",
            }
        )


def test_success_response_cannot_contain_error_fields() -> None:
    with pytest.raises(ValidationError):
        McpToolResponse.model_validate(
            {
                "ok": True,
                "data": {},
                "evidence": [],
                "error_code": "TIMEOUT",
                "error_message": "unexpected",
                "observed_at": "2026-07-10T02:00:00+08:00",
            }
        )


def test_failed_response_requires_error_code_and_message() -> None:
    with pytest.raises(ValidationError):
        McpToolResponse.model_validate(
            {
                "ok": False,
                "data": {},
                "evidence": [],
                "observed_at": "2026-07-10T02:00:00+08:00",
            }
        )


def test_only_transient_errors_are_retryable() -> None:
    assert RETRYABLE_TOOL_ERRORS == {
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.SERVICE_UNAVAILABLE,
    }
    assert ToolErrorCode.PERMISSION_DENIED not in RETRYABLE_TOOL_ERRORS
