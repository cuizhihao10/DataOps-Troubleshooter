from __future__ import annotations

from datetime import UTC, datetime

from app.domain.planner import ToolAction
from app.domain.tooling import McpToolResponse
from app.mcp.client import McpClientError, StdioMcpClient
from app.mcp.observation import ToolObservation, normalize_observation


class McpToolExecutor:
    def __init__(self, client: StdioMcpClient) -> None:
        self._client = client

    async def execute(self, action: ToolAction, *, attempt: int = 1) -> ToolObservation:
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
