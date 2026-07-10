from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.domain.tooling import ToolName
from mcp_server.tools import bds, lts

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
    )
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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
