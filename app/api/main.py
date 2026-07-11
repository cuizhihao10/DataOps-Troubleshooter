"""FastAPI 应用入口和启动时依赖审计。

lifespan 会在开放端口前校验 Fixture、Golden Case、Prompt、九个 MCP 工具以及可选
PostgreSQL 图数据。依赖不完整时直接拒绝启动，避免用户提交诊断后才遇到隐蔽配置错误。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app import __version__
from app.agents.auditor_chat import AUDITOR_PROVIDER_CONTRACT_ID
from app.agents.chat import PLANNER_PROVIDER_CONTRACT_ID
from app.agents.factory import create_auditor_runtime, create_planner_runtime
from app.agents.prompts import (
    AUDITOR_PROMPT_ID,
    PLANNER_PROMPT_ID,
    load_auditor_prompt,
    load_planner_prompt,
)
from app.capabilities import CAPABILITY_CONTRACT_ID, get_capability_registry
from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.core.settings import get_settings
from app.domain.models import CaseMemory
from app.domain.tooling import ToolName
from app.mcp.client import StdioMcpClient
from app.memory import (
    CASE_MEMORY_CONTRACT_ID,
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    PostgresMemoryRuntime,
)
from app.orchestration import AUDITED_REPORT_WORKFLOW_CONTRACT_ID, REACT_LOOP_CONTRACT_ID
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
    auditor_prompt: str
    auditor_provider: str
    mcp: str
    golden_case: str
    runtime_capabilities: str
    react_loop: str
    audited_report_workflow: str
    case_memory: str
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


class AuditorConfiguration(BaseModel):
    """公开 Auditor Provider 的非敏感配置和启用状态。

    Auditor 与 Planner 使用相同端点/模型但拥有独立 Prompt、Schema 和修复预算；响应不包含 API
    key 或完整认证 URL。configured 仅表示本地运行时可构造，不冒充已请求远端模型。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "configured"]
    provider: str
    model: str
    endpoint_host: str
    timeout_seconds: float
    schema_repair_count: int


class MemoryConfiguration(BaseModel):
    """公开长期记忆存储状态、向量空间、去重阈值和三类状态计数。

    响应不包含案例正文、embedding 或数据库 URL；disabled 表示未配置 PostgreSQL，因此记忆 API
    返回 503。Provider/维度与 GraphRAG 共用同一已验证 Embedding 空间。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "ok"]
    contract_id: str
    embedding_provider: str
    embedding_dimensions: int
    dedup_similarity_threshold: float
    default_search_limit: int
    counts: MemoryCounts


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
    auditor: AuditorConfiguration
    memory: MemoryConfiguration
    retrieval: RetrievalConfiguration


class MemoryDecisionRequest(BaseModel):
    """定义用户对指定案例执行 confirm 或 reject 的显式请求体。

    有限枚举阻止任意状态字符串；接口路径沿用产品基线 `/confirm`，body 决定确认或拒绝，便于同一
    审计入口支持纠错和取消确认。
    """

    model_config = ConfigDict(extra="forbid")

    decision: MemoryDecision


class MemoryDecisionResponse(BaseModel):
    """返回记忆契约版本和状态转换后的完整 CaseMemory。

    响应不包含 embedding 或 ORM 字段；未命中由路由返回 404，不使用空 memory 模糊表示。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    memory: CaseMemory


class MemorySearchResponse(BaseModel):
    """返回查询文本和仅包含 confirmed 案例的有界相似度列表。

    Pydantic `CaseMemoryMatch` 会再次拒绝 pending/rejected；query 原样回显便于演示和审计，不包含
    查询 embedding。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    query: str = Field(min_length=1, max_length=2000)
    matches: list[CaseMemoryMatch]


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
    if settings.auditor_prompt_id != AUDITOR_PROMPT_ID:
        raise ValueError("configured Auditor prompt ID does not match the packaged prompt")
    if not load_auditor_prompt().strip():
        raise ValueError("Auditor prompt must not be empty")
    if settings.auditor_provider_contract_id != AUDITOR_PROVIDER_CONTRACT_ID:
        raise ValueError("configured Auditor provider contract ID does not match the package")
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
    if settings.audited_report_workflow_contract_id != AUDITED_REPORT_WORKFLOW_CONTRACT_ID:
        raise ValueError(
            "configured audited report workflow contract ID does not match the package"
        )
    if settings.case_memory_contract_id != CASE_MEMORY_CONTRACT_ID:
        raise ValueError("configured case memory contract ID does not match the package")

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
    planner_runtime = None
    auditor_runtime = None
    memory_runtime = None
    memory_counts = MemoryCounts(pending=0, confirmed=0, rejected=0)
    try:
        if settings.database_url is not None:
            # 数据库是可选依赖：纯单测模式标记 disabled；配置后则必须真正连接并查询。
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
            memory_runtime = PostgresMemoryRuntime(
                factory,
                embedding_provider,
                dedup_similarity_threshold=settings.memory_dedup_similarity_threshold,
                default_search_limit=settings.memory_search_limit,
            )
            memory_counts = await memory_runtime.counts()
            database_status = "ok"

        # 在数据库审计后构造两个模型角色；若第二个失败，finally 会关闭已经创建的第一个。
        # disabled 返回 None；启用时只创建 SDK/Prompt 边界，不发送付费或有副作用的探测请求。
        planner_runtime = create_planner_runtime(settings)
        auditor_runtime = create_auditor_runtime(settings)

        # 只有全部检查完成后才发布共享状态，避免路由观察到半初始化的依赖集合。
        app.state.settings = settings
        app.state.fixture_registry = fixture_registry
        app.state.golden_cases = golden_cases
        app.state.mcp_tools_available = mcp_tools_available
        app.state.capability_registry = capability_registry
        app.state.planner_runtime = planner_runtime
        app.state.auditor_runtime = auditor_runtime
        app.state.memory_runtime = memory_runtime
        app.state.memory_counts = memory_counts
        app.state.database_engine = database_engine
        app.state.database_status = database_status
        app.state.knowledge_nodes_loaded = knowledge_nodes_loaded
        app.state.knowledge_edges_loaded = knowledge_edges_loaded
        app.state.knowledge_nodes_embedded = knowledge_nodes_embedded
        yield
    finally:
        # 先按角色关闭模型 HTTP 池，再释放数据库池；均不吞异常，避免测试重启后遗留资源。
        if auditor_runtime is not None:
            await auditor_runtime.aclose()
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
            auditor_prompt=settings.auditor_prompt_id,
            auditor_provider=settings.auditor_provider_contract_id,
            mcp=settings.mcp_contract_id,
            golden_case=settings.golden_case_contract_id,
            runtime_capabilities=settings.capabilities_contract_id,
            react_loop=settings.react_loop_contract_id,
            audited_report_workflow=settings.audited_report_workflow_contract_id,
            case_memory=settings.case_memory_contract_id,
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
        auditor=AuditorConfiguration(
            status=("disabled" if request.app.state.auditor_runtime is None else "configured"),
            provider=settings.chat_provider,
            model=settings.chat_model,
            endpoint_host=settings.chat_base_url.host or "",
            timeout_seconds=settings.chat_timeout_seconds,
            schema_repair_count=settings.auditor_schema_repair_count,
        ),
        memory=MemoryConfiguration(
            status=("disabled" if request.app.state.memory_runtime is None else "ok"),
            contract_id=settings.case_memory_contract_id,
            embedding_provider=settings.embedding_provider,
            embedding_dimensions=settings.embedding_dimensions,
            dedup_similarity_threshold=settings.memory_dedup_similarity_threshold,
            default_search_limit=settings.memory_search_limit,
            counts=request.app.state.memory_counts,
        ),
        retrieval=RetrievalConfiguration(
            embedding_provider=settings.embedding_provider,
            embedding_dimensions=settings.embedding_dimensions,
            score_weights=settings.hybrid_scoring_weights(),
            evidence_budget=settings.evidence_bundle_budget(),
        ),
    )


@app.post(
    "/api/v1/memories/{memory_id}/confirm",
    response_model=MemoryDecisionResponse,
)
async def decide_memory(
    memory_id: str,
    payload: MemoryDecisionRequest,
    request: Request,
) -> MemoryDecisionResponse:
    """确认或拒绝一个案例记忆，并返回状态转换后的结构化对象。

    路由要求 PostgreSQL memory runtime 已启用，否则 503；不存在的 ID 返回 404。事务、行锁和状态
    更新时间由 runtime/service 管理，API 不直接操作 ORM 或允许恢复 pending。
    """

    runtime = _require_memory_runtime(request)
    memory = await runtime.decide(memory_id, payload.decision)
    if memory is None:
        raise HTTPException(status_code=404, detail="case memory not found")
    # 每次决策后刷新健康快照，使高频 /health 不需要自己打开数据库连接。
    request.app.state.memory_counts = await runtime.counts()
    return MemoryDecisionResponse(
        contract_id=CASE_MEMORY_CONTRACT_ID,
        memory=memory,
    )


@app.get(
    "/api/v1/memories/search",
    response_model=MemorySearchResponse,
)
async def search_memories(
    request: Request,
    query: str = Query(min_length=1, max_length=2000, pattern=r".*\S.*"),
    limit: int | None = Query(default=None, ge=1, le=20),
) -> MemorySearchResponse:
    """按自然语言查询 pgvector，并只返回 confirmed 案例。

    limit 缺省使用集中配置；query 必须至少含一个非空白字符，避免 Service 的领域 ValueError
    越过 HTTP 校验变成 500。pending/rejected 在 SQL 层排除并由响应模型再次校验。数据库未配置时
    返回 503，Provider 或 SQL 异常不吞掉为假空结果。
    """

    runtime = _require_memory_runtime(request)
    matches = await runtime.search(query, limit=limit)
    return MemorySearchResponse(
        contract_id=CASE_MEMORY_CONTRACT_ID,
        query=query,
        matches=matches,
    )


def _require_memory_runtime(request: Request) -> PostgresMemoryRuntime:
    """读取 lifespan 发布的 memory runtime，未配置 PostgreSQL 时抛出 HTTP 503。

    该检查集中两个路由的降级语义，避免 AttributeError 或把禁用存储误报为空搜索；测试可注入满足
    相同方法的 runtime 替身验证 HTTP Schema。
    """

    runtime = request.app.state.memory_runtime
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="case memory requires configured PostgreSQL",
        )
    return runtime
