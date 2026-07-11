"""FastAPI 应用入口和启动时依赖审计。

lifespan 会在开放端口前校验 Fixture、Golden Case、Prompt、九个 MCP 工具以及可选
PostgreSQL 图数据。依赖不完整时直接拒绝启动，避免用户提交诊断后才遇到隐蔽配置错误。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from app import __version__
from app.agents.chat import PLANNER_PROVIDER_CONTRACT_ID
from app.agents.factory import create_planner_runtime
from app.agents.prompts import PLANNER_PROMPT_ID, load_planner_prompt
from app.capabilities import CAPABILITY_CONTRACT_ID, get_capability_registry
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.core.settings import get_settings
from app.domain.tooling import ToolName
from app.mcp.client import StdioMcpClient
from app.orchestration import REACT_LOOP_CONTRACT_ID
from app.persistence.database import (
    check_database_connection,
    create_database_engine,
    create_session_factory,
)
from app.retrieval.embeddings import create_embedding_provider
from app.retrieval.models import (
    GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID,
    GRAPH_RETRIEVAL_CONTRACT_ID,
    EvidenceBundleBudget,
    HybridScoringWeights,
)
from app.retrieval.repository import PostgresGraphRepository


class ContractVersions(BaseModel):
    """描述健康检查公开的 Prompt、工具、评测、capability、ReAct 与 GraphRAG 契约标识。

    客户端可判断 Planner、MCP、Golden Case、固定能力、LangGraph 循环和两类检索上下文是否
    与预期环境一致；严格额外字段策略避免展示脚本静默依赖已经漂移的响应。
    """

    model_config = ConfigDict(extra="forbid")

    planner_prompt: str
    planner_provider: str
    mcp: str
    golden_case: str
    runtime_capabilities: str
    react_loop: str
    graph_retrieval: str
    graph_evidence_bundle: str


class RuntimeLimits(BaseModel):
    """公开影响诊断成本和终止条件的集中式运行预算。

    这些值来自经过 Pydantic 校验的 Settings，而不是散落在节点中的魔法数字；Action 数和
    总墙钟分别限制循环深度与整体等待时间，健康接口公开它们以确认当前实例安全边界。
    """

    model_config = ConfigDict(extra="forbid")

    max_react_steps: int
    react_total_timeout_seconds: float
    max_graph_hops: int
    max_audit_revisions: int
    tool_retry_count: int


class RetrievalConfiguration(BaseModel):
    """公开当前 Embedding 空间、混合评分和 Evidence Bundle 预算，不含模型凭据。

    Provider ID、维度、权重和预算让演示者解释检索空间、排序公式和上下文上限；响应只来自经过
    Settings/Provider 工厂校验的值，避免健康接口报告运行时无法创建或无法满足的配置。
    """

    model_config = ConfigDict(extra="forbid")

    embedding_provider: str
    embedding_dimensions: int
    score_weights: HybridScoringWeights
    evidence_budget: EvidenceBundleBudget


class PlannerConfiguration(BaseModel):
    """公开 Planner Provider 的非敏感配置与启用状态。

    响应只包含 disabled/configured、Provider ID、模型、端点主机、超时和修复预算，不包含 API key、
    URL 用户信息或远端响应。configured 表示本地配置可构造，不冒充远端连接已经探测成功。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "configured"]
    provider: str
    model: str
    endpoint_host: str
    timeout_seconds: float
    schema_repair_count: int


class HealthResponse(BaseModel):
    """定义 `/health` 返回的已验证依赖、数据规模与契约快照。

    模型只报告可公开状态，不包含数据库 URL、凭据或原始 Fixture 内容。严格 Schema 让
    Docker 健康检查、集成测试和演示 UI 共享同一可机器验证的启动完成信号。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    service: str
    version: str
    environment: str
    fixtures_loaded: int
    golden_cases_loaded: int
    scenario_ids: list[str]
    mcp_tools_available: list[str]
    capabilities_available: list[str]
    database_status: Literal["disabled", "ok"]
    knowledge_nodes_loaded: int
    knowledge_edges_loaded: int
    knowledge_nodes_embedded: int
    contracts: ContractVersions
    limits: RuntimeLimits
    planner: PlannerConfiguration
    retrieval: RetrievalConfiguration


@asynccontextmanager
async def lifespan(app: FastAPI):
    """在 FastAPI 接流量前审计强依赖，并在停机时释放数据库连接池。

    启动阶段依次校验本地合成数据、版本化 Prompt/capability/ReAct 契约、真实 MCP 工具发现
    以及可选 PostgreSQL 图数据；任一步失败都会中止启动，使错误靠近配置源。通过检查后只把
    已验证对象写入 `app.state` 供路由只读复用；退出时始终关闭异步连接池，避免遗留连接。
    """

    settings = get_settings()

    # 先验证完全本地且成本最低的资产，让路径或 Schema 错误在启动早期给出清晰反馈。
    fixture_registry = FixtureRegistry.from_directory(settings.fixture_directory)
    golden_cases = load_golden_cases(settings.golden_case_file)
    scenario_ids = set(fixture_registry.scenario_ids)
    missing_scenarios = sorted({case.scenario_id for case in golden_cases} - scenario_ids)
    if missing_scenarios:
        raise ValueError(f"golden cases reference unknown scenarios: {missing_scenarios}")

    # Prompt 文本和 ID 必须成对校验，否则评测记录的版本无法代表实际执行内容。
    if settings.planner_prompt_id != PLANNER_PROMPT_ID:
        raise ValueError("configured planner prompt ID does not match the packaged prompt")
    if not load_planner_prompt().strip():
        raise ValueError("planner prompt must not be empty")
    if settings.planner_provider_contract_id != PLANNER_PROVIDER_CONTRACT_ID:
        raise ValueError("configured Planner provider contract ID does not match the package")
    if settings.graphrag_retrieval_contract_id != GRAPH_RETRIEVAL_CONTRACT_ID:
        raise ValueError("configured GraphRAG retrieval contract ID does not match the package")
    if settings.graphrag_evidence_bundle_contract_id != GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID:
        raise ValueError(
            "configured GraphRAG evidence bundle contract ID does not match the package"
        )

    # capability 注册表是 Planner 的策略边界，必须在模型或工具初始化前完成固定集合审计。
    capability_registry = get_capability_registry()
    if settings.capabilities_contract_id != CAPABILITY_CONTRACT_ID:
        raise ValueError("configured capability contract ID does not match the package")
    if settings.react_loop_contract_id != REACT_LOOP_CONTRACT_ID:
        raise ValueError("configured ReAct loop contract ID does not match the package")

    # Provider 工厂在任何部署模式都执行，使未知 ID 或非法维度不能等到首次检索才失败。
    embedding_provider = create_embedding_provider(
        settings.embedding_provider,
        dimensions=settings.embedding_dimensions,
    )

    # 工具发现必须跨真实 stdio MCP 握手；直接比较本地枚举会掩盖服务进程注册失败。
    mcp_client = StdioMcpClient(timeout_seconds=settings.tool_timeout_seconds)
    mcp_tools_available = await mcp_client.list_tools()
    required_mcp_tools = {tool.value for tool in ToolName}
    missing_mcp_tools = sorted(required_mcp_tools - set(mcp_tools_available))
    if missing_mcp_tools:
        raise ValueError(f"required MCP tools are unavailable: {missing_mcp_tools}")

    database_engine = None
    database_status = "disabled"
    knowledge_nodes_loaded = 0
    knowledge_edges_loaded = 0
    knowledge_nodes_embedded = 0
    if settings.database_url is not None:
        # 数据库是可选依赖：纯单测模式明确标记 disabled，配置后则必须真正可连接且可查询。
        database_engine = create_database_engine(settings.database_url.get_secret_value())
        await check_database_connection(database_engine)
        factory = create_session_factory(database_engine)
        async with factory() as session:
            repository = PostgresGraphRepository(session)
            knowledge_nodes_loaded, knowledge_edges_loaded = await repository.count_graph()
            knowledge_nodes_embedded = await repository.count_embedded_nodes(
                provider_id=embedding_provider.provider_id,
                dimensions=embedding_provider.dimensions,
            )
            if knowledge_nodes_embedded != knowledge_nodes_loaded:
                raise ValueError(
                    "all knowledge nodes must be embedded in the configured provider space"
                )
        database_status = "ok"

    # 放在所有远程/数据库启动检查之后构造，避免后续初始化失败遗留尚未进入 lifespan 的 HTTP 池。
    # disabled 返回 None；启用时只构造 SDK/Prompt 边界，不发送付费或有副作用的探测请求。
    planner_runtime = create_planner_runtime(settings)

    # 只有所有检查完成后才发布共享状态，避免路由观察到半初始化的依赖集合。
    app.state.settings = settings
    app.state.fixture_registry = fixture_registry
    app.state.golden_cases = golden_cases
    app.state.mcp_tools_available = mcp_tools_available
    app.state.capability_registry = capability_registry
    app.state.planner_runtime = planner_runtime
    app.state.database_engine = database_engine
    app.state.database_status = database_status
    app.state.knowledge_nodes_loaded = knowledge_nodes_loaded
    app.state.knowledge_edges_loaded = knowledge_edges_loaded
    app.state.knowledge_nodes_embedded = knowledge_nodes_embedded
    try:
        yield
    finally:
        # 先关闭模型 HTTP 池，再释放数据库池；两者均不吞异常，避免测试重启后遗留资源。
        if planner_runtime is not None:
            await planner_runtime.aclose()
        if database_engine is not None:
            await database_engine.dispose()


app = FastAPI(
    title="DataOps Troubleshooter",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """返回 lifespan 已验证并缓存的运行时健康快照。

    路由不重复执行磁盘、MCP 或数据库 I/O，因此健康探针可高频调用且不会放大下游压力；
    所有字段从初始化完成的 `app.state` 和集中配置组装，再由 `HealthResponse` 做最终边界校验。
    若 lifespan 未成功完成，FastAPI 不会开放该路由，因此无需在此伪造降级健康状态。
    """

    settings = request.app.state.settings
    fixture_registry = request.app.state.fixture_registry
    golden_cases = request.app.state.golden_cases
    mcp_tools_available = request.app.state.mcp_tools_available
    capability_registry = request.app.state.capability_registry
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        fixtures_loaded=len(fixture_registry),
        golden_cases_loaded=len(golden_cases),
        scenario_ids=list(fixture_registry.scenario_ids),
        mcp_tools_available=list(mcp_tools_available),
        capabilities_available=[
            definition.name.value for definition in capability_registry.definitions()
        ],
        database_status=request.app.state.database_status,
        knowledge_nodes_loaded=request.app.state.knowledge_nodes_loaded,
        knowledge_edges_loaded=request.app.state.knowledge_edges_loaded,
        knowledge_nodes_embedded=request.app.state.knowledge_nodes_embedded,
        contracts=ContractVersions(
            planner_prompt=settings.planner_prompt_id,
            planner_provider=settings.planner_provider_contract_id,
            mcp=settings.mcp_contract_id,
            golden_case=settings.golden_case_contract_id,
            runtime_capabilities=settings.capabilities_contract_id,
            react_loop=settings.react_loop_contract_id,
            graph_retrieval=settings.graphrag_retrieval_contract_id,
            graph_evidence_bundle=settings.graphrag_evidence_bundle_contract_id,
        ),
        limits=RuntimeLimits(
            max_react_steps=settings.max_react_steps,
            react_total_timeout_seconds=settings.react_total_timeout_seconds,
            max_graph_hops=settings.max_graph_hops,
            max_audit_revisions=settings.max_audit_revisions,
            tool_retry_count=settings.tool_retry_count,
        ),
        planner=PlannerConfiguration(
            status=("disabled" if request.app.state.planner_runtime is None else "configured"),
            provider=settings.chat_provider,
            model=settings.chat_model,
            endpoint_host=settings.chat_base_url.host or "",
            timeout_seconds=settings.chat_timeout_seconds,
            schema_repair_count=settings.planner_schema_repair_count,
        ),
        retrieval=RetrievalConfiguration(
            embedding_provider=settings.embedding_provider,
            embedding_dimensions=settings.embedding_dimensions,
            score_weights=settings.hybrid_scoring_weights(),
            evidence_budget=settings.evidence_bundle_budget(),
        ),
    )
