from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict

from app.domain.tooling import McpToolRequest, McpToolResponse, ToolErrorCode, ToolName


class McpClientError(RuntimeError):
    def __init__(self, message: str, error_code: ToolErrorCode) -> None:
        super().__init__(message)
        self.error_code = error_code


class McpToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    read_only: bool
    destructive: bool
    idempotent: bool
    has_output_schema: bool


class StdioMcpClient:
    def __init__(
        self,
        *,
        server_module: str = "mcp_server.server",
        timeout_seconds: float = 5,
        cwd: Path | None = None,
    ) -> None:
        self._server_module = server_module
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd or Path.cwd()

    async def list_tools(self) -> tuple[str, ...]:
        descriptors = await self.list_tool_descriptors()
        return tuple(descriptor.name for descriptor in descriptors)

    async def list_tool_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session() as session:
                    result = await session.list_tools()
        except TimeoutError as exc:
            raise McpClientError("MCP list_tools timed out", ToolErrorCode.TIMEOUT) from exc
        except McpClientError:
            raise
        except Exception as exc:
            raise McpClientError(
                f"MCP list_tools transport failed: {exc}",
                ToolErrorCode.SERVICE_UNAVAILABLE,
            ) from exc
        return tuple(
            sorted(
                (
                    McpToolDescriptor(
                        name=tool.name,
                        read_only=bool(tool.annotations and tool.annotations.readOnlyHint),
                        destructive=bool(tool.annotations and tool.annotations.destructiveHint),
                        idempotent=bool(tool.annotations and tool.annotations.idempotentHint),
                        has_output_schema=tool.outputSchema is not None,
                    )
                    for tool in result.tools
                ),
                key=lambda descriptor: descriptor.name,
            )
        )

    async def call_tool(
        self,
        tool_name: ToolName,
        request: McpToolRequest,
    ) -> McpToolResponse:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session() as session:
                    result = await session.call_tool(
                        tool_name.value,
                        arguments=request.model_dump(mode="json"),
                        read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                    )
        except TimeoutError as exc:
            raise McpClientError(
                f"MCP tool {tool_name.value} timed out",
                ToolErrorCode.TIMEOUT,
            ) from exc
        except McpClientError:
            raise
        except Exception as exc:
            raise McpClientError(
                f"MCP tool {tool_name.value} transport failed: {exc}",
                ToolErrorCode.SERVICE_UNAVAILABLE,
            ) from exc

        payload = _extract_payload(result)
        return McpToolResponse.model_validate(payload)

    def _server_parameters(self) -> StdioServerParameters:
        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self._server_module],
            env=environment,
            cwd=str(self._cwd),
            encoding="utf-8",
            encoding_error_handler="strict",
        )

    def _session(self):
        return _McpSessionContext(self._server_parameters())


class _McpSessionContext:
    def __init__(self, parameters: StdioServerParameters) -> None:
        self._parameters = parameters
        self._stdio_context = None
        self._session_context = None

    async def __aenter__(self) -> ClientSession:
        self._stdio_context = stdio_client(self._parameters, errlog=sys.stderr)
        read_stream, write_stream = await self._stdio_context.__aenter__()
        self._session_context = ClientSession(read_stream, write_stream)
        session = await self._session_context.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, traceback)
        if self._stdio_context is not None:
            await self._stdio_context.__aexit__(exc_type, exc, traceback)


def _extract_payload(result: CallToolResult) -> dict[str, Any]:
    if result.isError:
        message = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        raise McpClientError(
            message or "MCP tool returned an error",
            ToolErrorCode.INTERNAL_ERROR,
        )

    if result.structuredContent is not None:
        return result.structuredContent

    for block in result.content:
        if not isinstance(block, TextContent):
            continue
        try:
            payload = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise McpClientError(
        "MCP tool returned no structured JSON payload",
        ToolErrorCode.INTERNAL_ERROR,
    )
