"""FastAPI 应用入口和启动时依赖审计。

lifespan 会在开放端口前校验 Fixture、Golden Case、Prompt、九个 MCP 工具以及可选 PostgreSQL
图/记忆/运行资源。只有数据库和两个模型角色都配置时才发布诊断资源 runtime；否则路由明确 503。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, status
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
from app.mcp.executor import McpToolExecutor
from app.memory import (
    CASE_MEMORY_CONTRACT_ID,
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    PostgresMemoryRuntime,
)
from app.memory.checkpoint import SESSION_CHECKPOINT_CONTRACT_ID
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    DIAGNOSIS_API_CONTRACT_ID,
    DIAGNOSIS_WORKFLOW_CONTRACT_ID,
    REACT_LOOP_CONTRACT_ID,
    AgentRunSnapshot,
    AuditedDiagnosisWorkflow,
    AuditedReportWorkflow,
    BoundedReactLoop,
    DiagnosisMessage,
    DiagnosisSession,
    DiagnosisWorkflowConfig,
    ReactLoopConfig,
    ReportWorkflowConfig,
    RunEventList,
)
from app.orchestration.diagnosis_runtime import (
    DiagnosisApplicationRuntime,
    DiagnosisExecutionFailed,
    PostgresGraphContextRetriever,
)
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
    """描述健康检查公开的 Prompt、工具、工作流、资源 API 与 GraphRAG 契约标识。

    客户端可判断 Planner、MCP、Golden Case、固定能力、三个 LangGraph 层和资源/检索上下文是否
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
    diagnosis_workflow: str
    diagnosis_api: str
    session_checkpoint: str
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
    """公开长期记忆存储状态、向量空间、去重/图关系/查询预算和三类状态计数。

    响应不包含案例正文、embedding 或数据库 URL；disabled 表示未配置 PostgreSQL，因此记忆 API
    返回 503。Provider/维度与 GraphRAG 共用同一已验证 Embedding 空间；独立图阈值连接未达到
    canonical 去重条件的 confirmed 案例，查询字符上限则约束顶层诊断组合历史检索文本的成本。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "ok"]
    contract_id: str
    embedding_provider: str
    embedding_dimensions: int
    dedup_similarity_threshold: float
    graph_similarity_threshold: float
    default_search_limit: int
    query_max_chars: int
    counts: MemoryCounts


class DiagnosisApiConfiguration(BaseModel):
    """公开资源化诊断 API 是否可执行、首版执行模式和 GraphRAG 种子预算。

    configured 要求 PostgreSQL、Planner 与 Auditor runtime 全部可构造；默认 disabled 不冒充模型已
    可用。execution_mode 明确首版在提交请求内同步完成，尚未宣称可靠后台队列或 checkpoint。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "configured"]
    contract_id: str
    checkpoint_contract_id: str
    execution_mode: Literal["synchronous"]
    retrieval_seed_limit: int


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
    diagnosis_api: DiagnosisApiConfiguration
    retrieval: RetrievalConfiguration


class SessionCreateRequest(BaseModel):
    """定义创建排障会话时可选的公开标题。

    默认标题便于最小客户端提交空 JSON；纯空白由字段正则和 runtime 双重拒绝。标题不作为 Prompt，
    也不包含用户完整问题，后续 message 单独持久化。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="新排障会话", min_length=1, max_length=200, pattern=r"\S")


class SessionCreateResponse(BaseModel):
    """返回资源契约版本和已提交 PostgreSQL 的会话快照。

    响应成功时 session 一定已持久化；不返回数据库内部行或未来 checkpoint 内容。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    session: DiagnosisSession


class MessageSubmissionResponse(BaseModel):
    """返回 message 触发的终态 run 快照和资源契约版本。

    首版同步执行，因此成功响应通常为 completed；若执行失败，路由返回包含 run_id 的 500，客户端
    仍可通过 GET run/events 查看已持久化安全错误。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    run: AgentRunSnapshot


class RunResponse(BaseModel):
    """封装 GET run 返回的强类型持久化快照。

    completed 携带完整结构化诊断结果，failed 只含安全错误，running 不含部分结果；状态组合由
    AgentRunSnapshot 校验。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    run: AgentRunSnapshot


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
    """返回查询文本和仅包含 confirmed 案例的有界向量/图融合候选列表。

    Pydantic `CaseMemoryMatch` 会再次拒绝 pending/rejected，并解释 vector/graph 通道、直接分、
    图传播分和 edge 引用；query 原样回显便于演示和审计，不包含查询 embedding。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    query: str = Field(min_length=1, max_length=2000)
    matches: list[CaseMemoryMatch]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """在 FastAPI 接流量前审计强依赖，并在停机时释放数据库连接池。

    启动阶段依次校验本地合成数据、版本化 Prompt/capability/workflow/API 契约、真实 MCP 工具
    发现和可选 PostgreSQL 数据；任一步失败都会中止启动。诊断 runtime 仅在数据库与两个模型角色
    同时可用时组装；退出时关闭模型 HTTP 池和数据库池，避免遗留连接。
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
    if settings.diagnosis_workflow_contract_id != DIAGNOSIS_WORKFLOW_CONTRACT_ID:
        raise ValueError("configured diagnosis workflow contract ID does not match the package")
    if settings.diagnosis_api_contract_id != DIAGNOSIS_API_CONTRACT_ID:
        raise ValueError("configured diagnosis API contract ID does not match the package")
    if settings.session_checkpoint_contract_id != SESSION_CHECKPOINT_CONTRACT_ID:
        raise ValueError("configured session checkpoint contract ID does not match the package")
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
    diagnosis_runtime = None
    session_factory = None
    memory_counts = MemoryCounts(pending=0, confirmed=0, rejected=0)
    try:
        if settings.database_url is not None:
            # 数据库是可选依赖：纯单测模式标记 disabled；配置后则必须真正连接并查询。
            database_engine = create_database_engine(settings.database_url.get_secret_value())
            await check_database_connection(database_engine)
            session_factory = create_session_factory(database_engine)
            async with session_factory() as session:
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
                session_factory,
                embedding_provider,
                dedup_similarity_threshold=settings.memory_dedup_similarity_threshold,
                default_search_limit=settings.memory_search_limit,
                graph_similarity_threshold=settings.case_graph_similarity_threshold,
            )
            memory_counts = await memory_runtime.counts()
            database_status = "ok"

        # 在数据库审计后构造两个模型角色；若第二个失败，finally 会关闭已经创建的第一个。
        # disabled 返回 None；启用时只创建 SDK/Prompt 边界，不发送付费或有副作用的探测请求。
        planner_runtime = create_planner_runtime(settings)
        auditor_runtime = create_auditor_runtime(settings)

        if (
            session_factory is not None
            and memory_runtime is not None
            and planner_runtime is not None
            and auditor_runtime is not None
        ):
            # 资源 runtime 只在数据库和两个模型角色都可构造时发布；构造本身不发送模型请求。
            retriever = PostgresGraphContextRetriever(
                session_factory,
                embedding_provider,
                score_weights=settings.hybrid_scoring_weights(),
                budget=settings.evidence_bundle_budget(),
                seed_limit=settings.diagnosis_retrieval_seed_limit,
                max_hops=settings.max_graph_hops,
            )
            diagnosis_workflow = AuditedDiagnosisWorkflow(
                react=BoundedReactLoop(
                    planner=planner_runtime.agent,
                    executor=McpToolExecutor(
                        mcp_client,
                        retry_count=settings.tool_retry_count,
                    ),
                    config=ReactLoopConfig(
                        max_steps=settings.max_react_steps,
                        total_timeout_seconds=settings.react_total_timeout_seconds,
                    ),
                    registry=capability_registry,
                ),
                report=AuditedReportWorkflow(
                    auditor=auditor_runtime.agent,
                    config=ReportWorkflowConfig(
                        max_revisions=settings.max_audit_revisions,
                    ),
                ),
                memory=memory_runtime,
                config=DiagnosisWorkflowConfig(
                    memory_search_limit=settings.memory_search_limit,
                    memory_query_max_chars=settings.memory_query_max_chars,
                ),
            )
            diagnosis_runtime = DiagnosisApplicationRuntime(
                session_factory,
                retriever=retriever,
                workflow=diagnosis_workflow,
            )

        # 只有全部检查完成后才发布共享状态，避免路由观察到半初始化的依赖集合。
        app.state.settings = settings
        app.state.fixture_registry = fixture_registry
        app.state.golden_cases = golden_cases
        app.state.mcp_tools_available = mcp_tools_available
        app.state.capability_registry = capability_registry
        app.state.planner_runtime = planner_runtime
        app.state.auditor_runtime = auditor_runtime
        app.state.memory_runtime = memory_runtime
        app.state.diagnosis_runtime = diagnosis_runtime
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
            diagnosis_workflow=settings.diagnosis_workflow_contract_id,
            diagnosis_api=settings.diagnosis_api_contract_id,
            session_checkpoint=settings.session_checkpoint_contract_id,
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
            graph_similarity_threshold=settings.case_graph_similarity_threshold,
            default_search_limit=settings.memory_search_limit,
            query_max_chars=settings.memory_query_max_chars,
            counts=request.app.state.memory_counts,
        ),
        diagnosis_api=DiagnosisApiConfiguration(
            status=("disabled" if request.app.state.diagnosis_runtime is None else "configured"),
            contract_id=settings.diagnosis_api_contract_id,
            checkpoint_contract_id=settings.session_checkpoint_contract_id,
            execution_mode="synchronous",
            retrieval_seed_limit=settings.diagnosis_retrieval_seed_limit,
        ),
        retrieval=RetrievalConfiguration(
            embedding_provider=settings.embedding_provider,
            embedding_dimensions=settings.embedding_dimensions,
            score_weights=settings.hybrid_scoring_weights(),
            evidence_budget=settings.evidence_bundle_budget(),
        ),
    )


@app.post(
    "/api/v1/sessions",
    response_model=SessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_diagnosis_session(
    payload: SessionCreateRequest,
    request: Request,
) -> SessionCreateResponse:
    """创建一个 PostgreSQL 持久化排障会话，diagnosis runtime 禁用时返回 503。

    路由不直接生成 ID 或操作 ORM；runtime 确保响应前事务已提交。Pydantic 先拒绝空标题，数据库
    异常继续传播给统一服务器错误边界。
    """

    runtime = _require_diagnosis_runtime(request)
    session = await runtime.create_session(title=payload.title)
    return SessionCreateResponse(contract_id=DIAGNOSIS_API_CONTRACT_ID, session=session)


@app.post(
    "/api/v1/sessions/{session_id}/messages",
    response_model=MessageSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_diagnosis_message(
    session_id: str,
    payload: DiagnosisMessage,
    request: Request,
) -> MessageSubmissionResponse:
    """提交消息并同步执行 GraphRAG、双 Agent、审计和记忆暂存，返回终态 run。

    会话不存在返回 404。workflow 失败时 runtime 已持久化 failed run/event，路由返回含 run_id 和
    稳定 error_code 的 500，不暴露原异常文本；成功结果可随后通过 GET 轮询复读。
    """

    runtime = _require_diagnosis_runtime(request)
    try:
        run = await runtime.submit_message(session_id, payload)
    except DiagnosisExecutionFailed as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "run_id": exc.run_id,
                "error_code": exc.error_code,
                "message": exc.public_message,
            },
        ) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="diagnosis session not found")
    return MessageSubmissionResponse(contract_id=DIAGNOSIS_API_CONTRACT_ID, run=run)


@app.get("/api/v1/runs/{run_id}", response_model=RunResponse)
async def get_diagnosis_run(run_id: str, request: Request) -> RunResponse:
    """读取已持久化 run 的 running/completed/failed 快照，未知 ID 返回 404。

    路由不重新执行 workflow 或加载事件；completed JSONB 会在仓储边界重新通过全部 Pydantic 契约，
    防止旧/损坏结果直接暴露。
    """

    runtime = _require_diagnosis_runtime(request)
    run = await runtime.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    return RunResponse(contract_id=DIAGNOSIS_API_CONTRACT_ID, run=run)


@app.get("/api/v1/runs/{run_id}/events", response_model=RunEventList)
async def get_diagnosis_run_events(run_id: str, request: Request) -> RunEventList:
    """按 sequence 返回检索、ReAct、报告、记忆或系统失败公开事件。

    未知 run 返回 404；响应不包含 Thought、Prompt、embedding 或原始异常。事件连续性由仓储排序和
    RunEventList 双重校验。
    """

    runtime = _require_diagnosis_runtime(request)
    events = await runtime.get_events(run_id)
    if events is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    return events


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


def _require_diagnosis_runtime(request: Request) -> DiagnosisApplicationRuntime:
    """读取 lifespan 发布的资源化诊断 runtime，依赖不完整时抛出 HTTP 503。

    runtime 只有在 PostgreSQL、Planner 和 Auditor 都配置后存在；集中检查防止四个路由分别产生
    AttributeError 或把 disabled 模式误报为未知 session/run。测试可注入满足同一方法的替身。
    """

    runtime = request.app.state.diagnosis_runtime
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="diagnosis resources require PostgreSQL and configured Planner/Auditor",
        )
    return runtime
