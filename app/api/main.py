from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from app import __version__
from app.agents.prompts import PLANNER_PROMPT_ID, load_planner_prompt
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.core.settings import get_settings
from app.domain.tooling import ToolName
from app.mcp.client import StdioMcpClient


class ContractVersions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_prompt: str
    mcp: str
    golden_case: str


class RuntimeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_react_steps: int
    max_graph_hops: int
    max_audit_revisions: int
    tool_retry_count: int


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    service: str
    version: str
    environment: str
    fixtures_loaded: int
    golden_cases_loaded: int
    scenario_ids: list[str]
    mcp_tools_available: list[str]
    contracts: ContractVersions
    limits: RuntimeLimits


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    fixture_registry = FixtureRegistry.from_directory(settings.fixture_directory)
    golden_cases = load_golden_cases(settings.golden_case_file)
    scenario_ids = set(fixture_registry.scenario_ids)
    missing_scenarios = sorted({case.scenario_id for case in golden_cases} - scenario_ids)
    if missing_scenarios:
        raise ValueError(f"golden cases reference unknown scenarios: {missing_scenarios}")
    if settings.planner_prompt_id != PLANNER_PROMPT_ID:
        raise ValueError("configured planner prompt ID does not match the packaged prompt")
    if not load_planner_prompt().strip():
        raise ValueError("planner prompt must not be empty")

    mcp_client = StdioMcpClient(timeout_seconds=settings.tool_timeout_seconds)
    mcp_tools_available = await mcp_client.list_tools()
    required_slice_tools = {
        ToolName.LTS_GET_TASK_STATUS.value,
        ToolName.LTS_GET_TASK_LOG.value,
        ToolName.LTS_GET_DEPENDENCY_TOPOLOGY.value,
    }
    missing_mcp_tools = sorted(required_slice_tools - set(mcp_tools_available))
    if missing_mcp_tools:
        raise ValueError(f"required MCP tools are unavailable: {missing_mcp_tools}")

    app.state.settings = settings
    app.state.fixture_registry = fixture_registry
    app.state.golden_cases = golden_cases
    app.state.mcp_tools_available = mcp_tools_available
    yield


app = FastAPI(
    title="DataOps Troubleshooter",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    fixture_registry = request.app.state.fixture_registry
    golden_cases = request.app.state.golden_cases
    mcp_tools_available = request.app.state.mcp_tools_available
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        fixtures_loaded=len(fixture_registry),
        golden_cases_loaded=len(golden_cases),
        scenario_ids=list(fixture_registry.scenario_ids),
        mcp_tools_available=list(mcp_tools_available),
        contracts=ContractVersions(
            planner_prompt=settings.planner_prompt_id,
            mcp=settings.mcp_contract_id,
            golden_case=settings.golden_case_contract_id,
        ),
        limits=RuntimeLimits(
            max_react_steps=settings.max_react_steps,
            max_graph_hops=settings.max_graph_hops,
            max_audit_revisions=settings.max_audit_revisions,
            tool_retry_count=settings.tool_retry_count,
        ),
    )
