from __future__ import annotations

from datetime import UTC, datetime

from app.domain.planner import ToolAction
from app.domain.tooling import RETRYABLE_TOOL_ERRORS, McpToolResponse
from app.mcp.client import McpClientError, StdioMcpClient
from app.mcp.observation import (
    ToolObservation,
    merge_observations,
    normalize_observation,
)


class McpToolExecutor:
    def __init__(self, client: StdioMcpClient, *, retry_count: int) -> None:
        if retry_count not in {0, 1}:
            raise ValueError("retry_count must be 0 or 1")
        self._client = client
        self._retry_count = retry_count

    async def execute(self, action: ToolAction) -> ToolObservation:
        observations: list[ToolObservation] = []
        for attempt in range(1, self._retry_count + 2):
            observation = await self._execute_once(action, attempt=attempt)
            observations.append(observation)
            if observation.response.error_code not in RETRYABLE_TOOL_ERRORS:
                break
        return merge_observations(observations)

    async def _execute_once(
        self,
        action: ToolAction,
        *,
        attempt: int,
    ) -> ToolObservation:
        started_at = datetime.now(UTC)
        try:
            response = await self._client.call_tool(action.tool_name, action.arguments)
        except McpClientError as exc:
            response = McpToolResponse(
                ok=False,
                data={},
                evidence=[],
                error_code=exc.error_code,
                error_message=str(exc)[:1000],
                observed_at=datetime.now(UTC),
            )
        completed_at = datetime.now(UTC)
        return normalize_observation(
            action=action,
            response=response,
            started_at=started_at,
            completed_at=completed_at,
            attempt=attempt,
        )
