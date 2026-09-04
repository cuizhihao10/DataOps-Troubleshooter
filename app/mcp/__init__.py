"""MCP 客户端、工具执行和 Observation 标准化边界。

Agent 只能依赖本包调用工具，不能打开 data/fixtures。该约束保证每次 Action 都经过真实
MCP 握手、参数 Schema、传输超时和 ToolEvent trace，而不是以本地函数伪造协议调用。

传输是配置项：生产形态走 `StreamableHttpMcpClient`（独立部署的运维网关），`StdioMcpClient`
保留为可选配置与代码学习路线。装配一律通过 `create_mcp_client`，调用方只依赖 `McpToolClient`。
"""

from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor
from app.mcp.factory import create_mcp_client
from app.mcp.observation import ToolObservation
from app.mcp.protocol import MCP_TRANSPORT_CONTRACT_ID, McpToolClient
from app.mcp.streamable_http import StreamableHttpMcpClient

__all__ = [
    "MCP_TRANSPORT_CONTRACT_ID",
    "McpToolClient",
    "McpToolExecutor",
    "StdioMcpClient",
    "StreamableHttpMcpClient",
    "ToolObservation",
    "create_mcp_client",
]
