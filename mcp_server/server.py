"""FastMCP stdio 服务和九个只读工具注册表。

所有工具共享只读、非破坏、幂等和封闭世界注解。注册表中的名称来自 ToolName 枚举，
API 启动与协议测试会反向检查九项齐全，防止静默改名或遗漏。
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.domain.tooling import ToolName
from mcp_server.tools import bds, flashsync, lts

mcp = FastMCP(
    name="dataops-troubleshooter-mock",
    instructions="Read-only synthetic tools for DataOps troubleshooting demonstrations.",
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
    """以 stdio transport 启动本地 FastMCP 服务并占用当前进程事件循环。

    stdio 让客户端通过标准 MCP 消息与独立子进程通信，不开放网络端口，也不会接入真实系统。
    该函数只在模块作为程序执行时调用；导入模块用于测试工具发现不会重复启动服务。
    """

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
