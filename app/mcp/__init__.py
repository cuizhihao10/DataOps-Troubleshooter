"""MCP 客户端、工具执行和 Observation 标准化边界。

Agent 只能依赖本包调用工具，不能打开 data/fixtures。该约束保证每次 Action 都经过真实
MCP 握手、参数 Schema、传输超时和 ToolEvent trace，而不是以本地函数伪造协议调用。
"""

from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor
from app.mcp.observation import ToolObservation

__all__ = ["McpToolExecutor", "StdioMcpClient", "ToolObservation"]
