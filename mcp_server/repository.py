"""MCP 服务端的 scenario_id Fixture 仓储。

只有本模块允许从注册表读取合成响应。未知场景和缺失工具结果被转换为统一错误响应，
因此客户端始终接收相同契约，而不会看到文件路径或 Python KeyError。
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from app.core.fixture_registry import FixtureRegistry
from app.core.settings import get_settings
from app.domain.tooling import (
    McpToolRequest,
    McpToolResponse,
    ToolErrorCode,
    ToolName,
)


class FixtureToolRepository:
    def __init__(self, registry: FixtureRegistry) -> None:
        self._registry = registry

    def execute(self, tool_name: ToolName, request: McpToolRequest) -> McpToolResponse:
        try:
            scenario = self._registry.get(request.scenario_id)
        except KeyError:
            return _error_response(
                ToolErrorCode.INVALID_REQUEST,
                f"unknown synthetic scenario: {request.scenario_id}",
            )

        for result in scenario.tool_results:
            if result.tool_name == tool_name and result.request.resource_id == request.resource_id:
                return result.response.model_copy(deep=True)

        return _error_response(
            ToolErrorCode.EMPTY_RESULT,
            (
                "no synthetic result matched tool "
                f"{tool_name.value} and resource {request.resource_id}"
            ),
        )


def _error_response(error_code: ToolErrorCode, message: str) -> McpToolResponse:
    return McpToolResponse(
        ok=False,
        data={},
        evidence=[],
        error_code=error_code,
        error_message=message,
        observed_at=datetime.now(UTC),
    )


@lru_cache
def get_fixture_tool_repository() -> FixtureToolRepository:
    settings = get_settings()
    registry = FixtureRegistry.from_directory(settings.fixture_directory)
    return FixtureToolRepository(registry)
