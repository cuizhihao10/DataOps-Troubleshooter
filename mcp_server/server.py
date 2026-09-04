"""FastMCP 服务、九个只读工具注册表与两条可选传输（stdio / Streamable HTTP）。

所有工具共享只读、非破坏、幂等和封闭世界注解。注册表中的名称来自 ToolName 枚举，
API 启动与协议测试会反向检查九项齐全，防止静默改名或遗漏。

生产形态是 Streamable HTTP：网关作为独立部署单元运行，鉴权与限流由 `mcp_server/security.py`
在 ASGI 层强制。stdio 保留为可选配置与"MCP 传输到底做了什么"的最短可读实现，不再为它扩展功能。
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from app.core.settings import Settings, get_settings
from app.domain.tooling import ToolName
from mcp_server.security import McpGatewaySecurityMiddleware, build_gateway_guard
from mcp_server.tools import bds, flashsync, lts

# stateless_http=True：每个请求新建一次性 transport，服务端不累积会话表，也不需要 DELETE 收尾，
# 横向扩容时更不需要粘性路由。客户端每次调用本来就新建 MCP 会话，因此这里不保留任何服务端状态是
# 语义一致的选择，而不是为了省事。
#
# DNS rebinding 防护必须**显式**关闭而不能靠不传参：FastMCP 构造函数在 host 属于
# 127.0.0.1/localhost/::1（默认值就是 127.0.0.1）时会自动开启它，并把 allowed_hosts 限死为三个
# 回环地址，于是任何按 service 名访问的请求（`Host: mcp-gateway:8900`）都会拿到 421。关闭的理由是
# 这层防护在本部署里保护不到任何东西：网关不发布宿主端口、不对浏览器开放，真正的门禁是 Bearer
# 令牌，而被 DNS rebinding 诱骗的浏览器拿不到令牌，请求会先被鉴权中间件挡成 401；反过来维护一份
# allowed_hosts 等于把部署地址抄第二遍，换 service 名、加 ingress 或上 k8s 时必然漂移。
mcp = FastMCP(
    name="dataops-troubleshooter-mock",
    instructions="Read-only synthetic tools for DataOps troubleshooting demonstrations.",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _register_tools() -> None:
    """把产品基线中的九个处理器注册为带安全注解的结构化 FastMCP 工具。

    元组集中绑定枚举名、展示标题、协议描述和处理函数，循环统一应用只读、非破坏、幂等、封闭
    世界注解及输出 Schema。若遗漏或改名，API 启动审计和协议集成测试会失败；本函数只注册，
    不执行工具或读取 Fixture。
    """

    tools = (
        (
            ToolName.LTS_GET_TASK_STATUS,
            "Get synthetic LTS task status",
            "Read deterministic LTS task status from a scenario fixture.",
            lts.get_task_status,
        ),
        (
            ToolName.LTS_GET_TASK_LOG,
            "Get synthetic LTS task log",
            "Read sanitized deterministic LTS task logs from a scenario fixture.",
            lts.get_task_log,
        ),
        (
            ToolName.LTS_GET_DEPENDENCY_TOPOLOGY,
            "Get synthetic LTS dependency topology",
            "Read deterministic LTS upstream and downstream dependencies.",
            lts.get_dependency_topology,
        ),
        (
            ToolName.BDS_GET_TASK_STATUS,
            "Get synthetic BDS task status",
            "Read deterministic BDS task status and resource usage evidence.",
            bds.get_task_status,
        ),
        (
            ToolName.BDS_GET_TASK_LOG,
            "Get synthetic BDS task log",
            "Read sanitized BDS logs, errors, and performance evidence.",
            bds.get_task_log,
        ),
        (
            ToolName.BDS_GET_TABLE_INFO,
            "Get synthetic BDS table information",
            "Read deterministic table structure, partition, and statistics evidence.",
            bds.get_table_info,
        ),
        (
            ToolName.FLASHSYNC_GET_SYNC_DELAY,
            "Get synthetic FlashSync delay",
            "Read deterministic synchronization delay, throughput, and backlog evidence.",
            flashsync.get_sync_delay,
        ),
        (
            ToolName.FLASHSYNC_GET_SYNC_LOG,
            "Get synthetic FlashSync log",
            "Read sanitized synchronization errors and conflict evidence.",
            flashsync.get_sync_log,
        ),
        (
            ToolName.FLASHSYNC_CHECK_CONSISTENCY,
            "Check synthetic FlashSync consistency",
            "Read deterministic source and target consistency sample evidence.",
            flashsync.check_consistency,
        ),
    )
    # 统一装饰过程避免九个处理器的安全注解逐处复制后发生配置漂移。
    for tool_name, title, description, handler in tools:
        mcp.tool(
            name=tool_name.value,
            title=title,
            description=description,
            annotations=READ_ONLY_ANNOTATIONS,
            structured_output=True,
        )(handler)


_register_tools()


def main() -> None:
    """按配置选择 stdio 或 Streamable HTTP 传输启动服务，并占用当前进程事件循环。

    传输是部署形态而不是代码分支：同一份工具注册表在两条传输上暴露完全相同的九个工具与注解，
    因此切换传输不需要改动任何工具实现。该函数只在模块作为程序执行时调用；导入模块用于测试
    工具发现不会启动任何传输，也不会打开网络端口。
    """

    settings = get_settings()
    if settings.mcp_transport == "stdio":
        # stdio 不开放网络端口，客户端以子进程方式通信，因此没有可鉴权的网络面。
        mcp.run(transport="stdio")
        return
    _run_streamable_http(settings)


def _run_streamable_http(settings: Settings) -> None:
    """把 FastMCP 的 Streamable HTTP 应用包上鉴权中间件后交给 uvicorn 运行。

    不用 `mcp.run(transport="streamable-http")`：那条路径会直接把未受保护的应用交给 uvicorn，
    中间件根本没有插入点。守卫在监听端口之前构造，因此弱令牌或缺令牌会让进程启动失败而不是
    先开始服务。uvicorn 采用惰性导入，让 stdio 与测试的导入路径不必拉起整套 HTTP 服务栈。
    """

    import uvicorn

    guard = build_gateway_guard(settings)
    application = McpGatewaySecurityMiddleware(mcp.streamable_http_app(), guard)
    uvicorn.run(
        application,
        host=settings.mcp_http_host,
        port=settings.mcp_http_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
