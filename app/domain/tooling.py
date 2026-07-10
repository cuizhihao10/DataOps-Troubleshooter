from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolName(StrEnum):
    LTS_GET_TASK_STATUS = "lts.get_task_status"
    LTS_GET_TASK_LOG = "lts.get_task_log"
    LTS_GET_DEPENDENCY_TOPOLOGY = "lts.get_dependency_topology"
    BDS_GET_TASK_STATUS = "bds.get_task_status"
    BDS_GET_TASK_LOG = "bds.get_task_log"
    BDS_GET_TABLE_INFO = "bds.get_table_info"
    FLASHSYNC_GET_SYNC_DELAY = "flashsync.get_sync_delay"
    FLASHSYNC_GET_SYNC_LOG = "flashsync.get_sync_log"
    FLASHSYNC_CHECK_CONSISTENCY = "flashsync.check_consistency"


class ToolErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    EMPTY_RESULT = "EMPTY_RESULT"
    TIMEOUT = "TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_range(self) -> TimeRange:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("time_range values must include a timezone")
        if self.end <= self.start:
            raise ValueError("time_range.end must be later than time_range.start")
        return self


class McpToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(min_length=1, max_length=200)
    time_range: TimeRange
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    trace_id: str = Field(min_length=3, max_length=100)


class ToolEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class McpToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[ToolEvidencePayload] = Field(default_factory=list)
    error_code: ToolErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=1000)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_success_or_error(self) -> McpToolResponse:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if self.ok and (self.error_code is not None or self.error_message is not None):
            raise ValueError("successful responses cannot include error fields")
        if not self.ok and (self.error_code is None or not self.error_message):
            raise ValueError("failed responses require error_code and error_message")
        return self


RETRYABLE_TOOL_ERRORS: frozenset[ToolErrorCode] = frozenset(
    {ToolErrorCode.TIMEOUT, ToolErrorCode.SERVICE_UNAVAILABLE}
)
