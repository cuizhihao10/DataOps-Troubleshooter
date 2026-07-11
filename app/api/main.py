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
from app.agents.prompts import PLANNER_PROMPT_ID, load_planner_prompt
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.core.settings import get_settings
from app.domain.tooling import ToolName
from app.mcp.client import StdioMcpClient
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
    """描述健康检查公开的 Prompt、工具、评测和两类 GraphRAG 契约标识。

    客户端可判断 Planner、MCP、Golden Case、完整检索和预算 Bundle 是否与预期环境一致；
    `extra="forbid"` 阻止服务端无意增加未约定字段，避免展示脚本静默依赖漂移后的响应。
    """

    model_config = ConfigDict(extra="forbid")

    planner_prompt: str
    mcp: str
    golden_case: str
    graph_retrieval: str
    graph_evidence_bundle: str


class RuntimeLimits(BaseModel):
    """公开影响诊断成本和终止条件的集中式运行预算。

    这些值来自经过 Pydantic 校验的 Settings，而不是散落在节点中的魔法数字；健康接口
    暴露预算便于面试演示和故障排查确认当前实例采用了哪组安全边界。
    """

    model_config = ConfigDict(extra="forbid")

    max_react_steps: int
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
    database_status: Literal["disabled", "ok"]
    knowledge_nodes_loaded: int
    knowledge_edges_loaded: int
    knowledge_nodes_embedded: int
    contracts: ContractVersions
    limits: RuntimeLimits
    retrieval: RetrievalConfiguration


@asynccontextmanager
async def lifespan(app: FastAPI):
    """在 FastAPI 接流量前审计强依赖，并在停机时释放数据库连接池。

    启动阶段依次校验本地合成数据、版本化 Prompt、真实 MCP 工具发现以及可选 PostgreSQL
    图数据；任一步失败都会中止启动，使错误靠近配置源而不是延迟到诊断请求。通过检查后，
    只把经过验证的对象写入 `app.state` 供路由只读复用。生成器退出时无论正常停机还是异常
    取消都会关闭异步连接池，防止测试重启或容器退出后遗留连接。
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
    if settings.graphrag_retrieval_contract_id != GRAPH_RETRIEVAL_CONTRACT_ID:
        raise ValueError("configured GraphRAG retrieval contract ID does not match the package")
    if settings.graphrag_evidence_bundle_contract_id != GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID:
        raise ValueError(
            "configured GraphRAG evidence bundle contract ID does not match the package"
        )

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

    # 只有所有检查完成后才发布共享状态，避免路由观察到半初始化的依赖集合。
    app.state.settings = settings
    app.state.fixture_registry = fixture_registry
    app.state.golden_cases = golden_cases
    app.state.mcp_tools_available = mcp_tools_available
    app.state.database_engine = database_engine
    app.state.database_status = database_status
    app.state.knowledge_nodes_loaded = knowledge_nodes_loaded
    app.state.knowledge_edges_loaded = knowledge_edges_loaded
    app.state.knowledge_nodes_embedded = knowledge_nodes_embedded
    try:
        yield
    finally:
        # dispose 会关闭连接池中的底层 asyncpg 连接；None 分支保持无数据库模式轻量可测。
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
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        fixtures_loaded=len(fixture_registry),
        golden_cases_loaded=len(golden_cases),
        scenario_ids=list(fixture_registry.scenario_ids),
        mcp_tools_available=list(mcp_tools_available),
        database_status=request.app.state.database_status,
        knowledge_nodes_loaded=request.app.state.knowledge_nodes_loaded,
        knowledge_edges_loaded=request.app.state.knowledge_edges_loaded,
        knowledge_nodes_embedded=request.app.state.knowledge_nodes_embedded,
        contracts=ContractVersions(
            planner_prompt=settings.planner_prompt_id,
            mcp=settings.mcp_contract_id,
            golden_case=settings.golden_case_contract_id,
            graph_retrieval=settings.graphrag_retrieval_contract_id,
            graph_evidence_bundle=settings.graphrag_evidence_bundle_contract_id,
        ),
        limits=RuntimeLimits(
            max_react_steps=settings.max_react_steps,
            max_graph_hops=settings.max_graph_hops,
            max_audit_revisions=settings.max_audit_revisions,
            tool_retry_count=settings.tool_retry_count,
        ),
        retrieval=RetrievalConfiguration(
            embedding_provider=settings.embedding_provider,
            embedding_dimensions=settings.embedding_dimensions,
            score_weights=settings.hybrid_scoring_weights(),
            evidence_budget=settings.evidence_bundle_budget(),
        ),
    )
