"""按配置选择 MCP 传输并构造客户端的工厂（`mcp-transport:v1`）。

工厂存在的意义是让"用哪条传输"成为一个部署配置，而不是散落在装配代码里的 import：调用方只拿到
`McpToolClient`，因此执行器、ReAct 循环和评测脚本都不知道自己在跟子进程还是跟一个远程网关说话。
两个实现的资源模型不同（stdio 拥有子进程，HTTP 拥有连接池），但都提供 `aclose` 语义上的收尾方式，
lifespan 因此可以统一处理。工厂不发起任何网络或子进程活动，真实握手留给启动阶段的工具发现。
"""

from __future__ import annotations

from app.core.settings import Settings
from app.mcp.client import StdioMcpClient
from app.mcp.protocol import McpToolClient
from app.mcp.streamable_http import StreamableHttpMcpClient


def create_mcp_client(settings: Settings) -> McpToolClient:
    """根据 `mcp_transport` 构造 stdio 或 Streamable HTTP 客户端。

    两条传输共用同一个工具超时预算，因此换传输不会悄悄改变 Action 的等待上限。HTTP 分支要求令牌
    存在：`Settings._validate_mcp_transport` 已经在启动阶段拦过，这里再拦一次是为了让工厂在被其它
    入口（评测脚本、独立巡检进程）直接调用时也不可能构造出一个匿名访问网关的客户端。
    """

    if settings.mcp_transport == "stdio":
        return StdioMcpClient(timeout_seconds=settings.tool_timeout_seconds)
    if settings.mcp_auth_token is None:
        raise ValueError("mcp_auth_token is required when mcp_transport is streamable-http")
    return StreamableHttpMcpClient(
        url=str(settings.mcp_http_url),
        auth_token=settings.mcp_auth_token,
        timeout_seconds=settings.tool_timeout_seconds,
    )
