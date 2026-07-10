from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.domain.tooling import ToolName
from mcp_server.tools.lts import get_task_status

mcp = FastMCP(
    name="dataops-troubleshooter-mock",
    instructions="Read-only synthetic tools for DataOps troubleshooting demonstrations.",
)

mcp.tool(
    name=ToolName.LTS_GET_TASK_STATUS.value,
    title="Get synthetic LTS task status",
    description="Read deterministic LTS task status from a scenario fixture.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)(get_task_status)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
