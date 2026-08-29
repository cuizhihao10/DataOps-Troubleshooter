"""FastAPI 应用入口和启动时依赖审计。

lifespan 会在开放端口前校验 Fixture、Golden Case、Prompt、九个 MCP 工具以及可选 PostgreSQL
图/记忆/运行资源。只有数据库和两个模型角色都配置时才发布诊断资源 runtime；否则路由明确 503。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette import EventSourceResponse

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
from app.agents.retrying import MODEL_TRANSIENT_RETRY_CONTRACT_ID
from app.api.security import (
    API_AUTH_CONTRACT_ID,
    PROTECTED_PATH_PREFIXES,
    ApiSecurityGuard,
    is_protected_path,
)
from app.api.streaming import (
    RUN_STREAM_CONTRACT_ID,
    RunStreamConfig,
    iter_run_stream,
    resolve_stream_cursor,
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
from app.observability import (
    RUN_TRACE_CONTRACT_ID,
    RunTrace,
    render_prometheus_text,
)
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    DIAGNOSIS_API_CONTRACT_ID,
    DIAGNOSIS_WORKFLOW_CONTRACT_ID,
    REACT_LOOP_CONTRACT_ID,
    AgentRunSnapshot,
    AgentRunStatus,
    AuditedDiagnosisWorkflow,
    AuditedReportWorkflow,
    BoundedReactLoop,
    DiagnosisMessage,
    DiagnosisRunWorker,
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
from app.orchestration.run_models import ActiveRunConflictError, RunResumeConflictError
from app.persistence.database import (
    check_database_connection,
    create_database_engine,
    create_session_factory,
)
from app.retrieval.document_repository import PostgresDocumentRepository
from app.retrieval.documents import (
    DOCUMENT_RETRIEVAL_CONTRACT_ID,
    DocumentScoringWeights,
)
from app.retrieval.embeddings import create_embedding_provider
from app.retrieval.models import (
    GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID,
    GRAPH_RETRIEVAL_CONTRACT_ID,
    EvidenceBundleBudget,
    HybridScoringWeights,
    KnowledgeNodeType,
)
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.reranker import create_reranker
from app.retrieval.seeds import load_knowledge_seed


class ContractVersions(BaseModel):
    """描述健康检查公开的 Prompt、工具、工作流、资源 API 与两条检索通道的契约标识。

    客户端可判断 Planner、MCP、Golden Case、固定能力、三个 LangGraph 层和资源/检索上下文是否
    与预期环境一致；文档检索单独列出契约，因为图通道与文档通道可以独立升版，把两者合成一个字段
    会让部署方无法判断"到底哪条知识通道发生了漂移"。严格额外字段策略避免展示脚本静默依赖
    已经漂移的响应。
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
    document_retrieval: str
    run_trace: str
    api_auth: str
    run_stream: str
    model_transient_retry: str


class RuntimeLimits(BaseModel):
    """公开影响诊断成本和终止条件的集中式运行预算。

    这些值来自经过 Pydantic 校验的 Settings，而不是散落在节点中的魔法数字；Action 数和
    总墙钟分别限制循环深度与整体等待时间，健康接口公开它们以确认当前实例安全边界。
    """

    model_config = ConfigDict(extra="forbid")

    max_react_steps: int
    # 并行上限和步数上限一起公开：只看 max_react_steps 无法判断这个实例是"六步串行"还是
    # "两轮各三个并行"，而这两者的 P95 延迟完全不同。
    max_parallel_tool_actions: int
    react_total_timeout_seconds: float
    max_graph_hops: int
    max_audit_revisions: int
    tool_retry_count: int
    # 模型侧重试次数与 tool_retry_count 并列公开：两者都是"瞬时失败最多再试几次"，但预算互相独立，
    # 只看工具侧会误判一个 429 是否会被直接判成 planner_provider_error 终态。
    chat_transient_retry_attempts: int


class RetrievalConfiguration(BaseModel):
    """公开当前 Embedding 空间、重排配置、两条通道的评分权重和 Evidence Bundle 预算，不含模型凭据。

    Provider ID、维度、权重和预算让演示者解释检索空间、排序公式和上下文上限；重排字段说明第二
    阶段是否启用、用哪个模型以及融合权重，使"名次为何变化"可以被外部核对。文档权重与图权重并列
    公开而不是合并，是因为两者因子集合本来不同（文档没有 path/freshness），合并展示会让人误以为
    文档片段也参与了关系路径打分。响应只来自经过 Settings/Provider 工厂校验的值，避免健康接口
    报告运行时无法创建或无法满足的配置。
    """

    model_config = ConfigDict(extra="forbid")

    embedding_provider: str
    embedding_dimensions: int
    embedding_model: str
    embedding_endpoint_host: str
    rerank_provider: str
    rerank_model: str
    rerank_endpoint_host: str
    rerank_candidate_multiplier: int
    rerank_blend_weight: float
    score_weights: HybridScoringWeights
    document_score_weights: DocumentScoringWeights
    document_chunk_limit: int
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
    execution_mode: Literal["postgres-worker"]
    worker_status: Literal["disabled", "running"]
    worker_poll_seconds: float
    worker_lease_seconds: float
    worker_heartbeat_seconds: float
    worker_max_attempts: int
    retrieval_seed_limit: int


class RunStreamConfiguration(BaseModel):
    """公开 SSE 推流的契约、轮询/心跳/寿命预算，以及鉴权模式下的已知限制。

    三个时间预算让演示者在打开前端之前就能解释"事件延迟大概多少、连接能活多久"；
    `available_under_auth` 则显式承认浏览器 `EventSource` 无法携带 Authorization 头，因此 bearer
    模式下推流一定会被鉴权中间件拒绝、前端必须退回轮询。把这个限制写进健康响应而不是只写在文档里，
    是为了避免演示时出现"说好有流式却一直在轮询"的解释成本。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    poll_seconds: float
    keepalive_seconds: float
    max_seconds: float
    available_under_auth: bool


class ApiSecurityConfiguration(BaseModel):
    """公开资源 API 的鉴权模式、受保护前缀与限流配额，不包含令牌或其摘要。

    `mode` 让运维在不试探接口的前提下确认这个实例是否需要令牌；受保护前缀显式列出，避免文档说
    保护 `/metrics` 而代码只保护 `/api/v1` 这类漂移。配额同时公开次数与窗口长度，因为只给出
    "120" 无法判断它是每分钟还是每秒。`/health` 自身不在受保护前缀内，否则容器存活探针需要凭据。
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["disabled", "bearer"]
    contract_id: str
    protected_path_prefixes: list[str]
    rate_limit_requests: int
    rate_limit_window_seconds: float


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
    documents_loaded: int
    document_chunks_loaded: int
    document_chunks_embedded: int
    contracts: ContractVersions
    limits: RuntimeLimits
    planner: PlannerConfiguration
    auditor: AuditorConfiguration
    memory: MemoryConfiguration
    diagnosis_api: DiagnosisApiConfiguration
    retrieval: RetrievalConfiguration
    security: ApiSecurityConfiguration
    stream: RunStreamConfiguration


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


class RunTraceResponse(BaseModel):
    """封装单次 run 的 per-run 调用链，供前端渲染时间轴与瓶颈定位。

    trace 契约与 run 快照分开返回，因为两者的消费者不同：run 面向诊断结论，trace 面向性能与可靠性
    分析。`dropped_span_count` 非零表示插桩超过上限被截断，必须原样暴露而不是让残缺 trace 看起来
    完整。响应结构由 `RunTrace` 再次校验父子顺序与唯一根。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    trace: RunTrace


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


class MemoryDeletionResponse(BaseModel):
    """返回永久删除操作的契约与删除结果。

    删除会同时移除 ``case_memories`` 行及其证据关联，并清理动态案例图节点；
    响应只暴露布尔结果，不把 embedding 或 ORM 内部字段泄露给浏览器。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    memory_id: str
    deleted: bool


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

    # 根因锚点必须真的是知识图里的 root_cause 节点：锚点一旦拼错就永远不可能被任何报告引用命中，
    # 于是 `root_cause_anchor_hit_rate` 会静默退化成恒为 0 的假指标——那正是这条契约要修掉的毛病。
    root_cause_node_ids = {
        node.node_id
        for node in load_knowledge_seed(settings.knowledge_seed_file).nodes
        if node.node_type is KnowledgeNodeType.ROOT_CAUSE
    }
    unknown_anchors = sorted(
        {
            anchor
            for case in golden_cases
            for anchor in case.allowed_root_cause_anchors
            if anchor not in root_cause_node_ids
        }
    )
    if unknown_anchors:
        raise ValueError(f"golden cases reference unknown root cause anchors: {unknown_anchors}")

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
    if settings.model_transient_retry_contract_id != MODEL_TRANSIENT_RETRY_CONTRACT_ID:
        raise ValueError("configured model transient retry contract ID does not match the package")
    if settings.graphrag_retrieval_contract_id != GRAPH_RETRIEVAL_CONTRACT_ID:
        raise ValueError("configured GraphRAG retrieval contract ID does not match the package")
    if settings.graphrag_evidence_bundle_contract_id != GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID:
        raise ValueError(
            "configured GraphRAG evidence bundle contract ID does not match the package"
        )
    if settings.document_retrieval_contract_id != DOCUMENT_RETRIEVAL_CONTRACT_ID:
        raise ValueError("configured document retrieval contract ID does not match the package")
    if settings.run_trace_contract_id != RUN_TRACE_CONTRACT_ID:
        raise ValueError("configured run trace contract ID does not match the package")
    if settings.api_auth_contract_id != API_AUTH_CONTRACT_ID:
        raise ValueError("configured API auth contract ID does not match the package")
    if settings.run_stream_contract_id != RUN_STREAM_CONTRACT_ID:
        raise ValueError("configured run stream contract ID does not match the package")

    # 守卫在任何 Provider、MCP 子进程和数据库连接之前构造：弱令牌或非法配额必须让进程拒绝开放
    # 端口，而不是等第一个请求到达时才发现"鉴权其实没生效"。
    api_security = ApiSecurityGuard(
        mode=settings.api_auth_mode,
        token=settings.api_auth_token,
        max_requests=settings.api_rate_limit_requests,
        window_seconds=settings.api_rate_limit_window_seconds,
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
        model=settings.embedding_model,
        base_url=str(settings.embedding_base_url),
        api_key=settings.embedding_api_key,
        timeout_seconds=settings.embedding_timeout_seconds,
        batch_size=settings.embedding_batch_size,
    )
    # 重排是可选第二阶段：`disabled` 返回 None，检索结果里的 reranker_model 因此真实为空，
    # 报告和评测不会把一阶段排序说成精排结果。未知 provider ID 在此立即失败。
    reranker = create_reranker(
        settings.rerank_provider,
        model=settings.rerank_model,
        base_url=str(settings.rerank_base_url),
        api_key=settings.rerank_api_key,
        timeout_seconds=settings.rerank_timeout_seconds,
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
    documents_loaded = 0
    document_chunks_loaded = 0
    document_chunks_embedded = 0
    planner_runtime = None
    auditor_runtime = None
    memory_runtime = None
    diagnosis_runtime = None
    diagnosis_worker = None
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
                document_repository = PostgresDocumentRepository(session)
                documents_loaded, document_chunks_loaded = (
                    await document_repository.count_documents()
                )
                document_chunks_embedded = await document_repository.count_embedded_chunks(
                    provider_id=embedding_provider.provider_id,
                    dimensions=embedding_provider.dimensions,
                )
                # 与知识节点同样是全有或全无：部分切片缺向量时语义通道只会少召回而不报错，
                # 那种"看起来正常但永远查不到某份 Runbook"的状态在演示中几乎不可能被发现。
                if document_chunks_embedded != document_chunks_loaded:
                    raise ValueError(
                        "all document chunks must be embedded in the configured provider space"
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
                reranker=reranker,
                rerank_candidate_multiplier=settings.rerank_candidate_multiplier,
                rerank_blend_weight=settings.rerank_blend_weight,
                document_score_weights=settings.document_scoring_weights(),
                document_chunk_limit=settings.document_retrieval_chunk_limit,
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
                        max_parallel_actions=settings.max_parallel_tool_actions,
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
            diagnosis_worker = DiagnosisRunWorker(
                diagnosis_runtime,
                session_factory,
                poll_interval_seconds=settings.diagnosis_worker_poll_seconds,
                lease_seconds=settings.diagnosis_worker_lease_seconds,
                heartbeat_seconds=settings.diagnosis_worker_heartbeat_seconds,
                max_attempts=settings.diagnosis_worker_max_attempts,
            )

        # 只有全部检查完成后才发布共享状态，避免路由观察到半初始化的依赖集合。
        app.state.settings = settings
        app.state.api_security = api_security
        app.state.fixture_registry = fixture_registry
        app.state.golden_cases = golden_cases
        app.state.mcp_tools_available = mcp_tools_available
        app.state.capability_registry = capability_registry
        app.state.planner_runtime = planner_runtime
        app.state.auditor_runtime = auditor_runtime
        app.state.memory_runtime = memory_runtime
        app.state.diagnosis_runtime = diagnosis_runtime
        app.state.diagnosis_worker = diagnosis_worker
        app.state.memory_counts = memory_counts
        app.state.database_engine = database_engine
        # 会话工厂与 Embedding Provider 公开给进程内工具（当前是真实模型评测的历史预置），使它们
        # 复用与检索完全同一个向量空间，而不是自己再造一个 Provider——ID 或维度一旦不同，pgvector
        # 就永远召回不到预置数据，而那种失败看起来和"模型没召回"一模一样。
        app.state.session_factory = session_factory
        app.state.embedding_provider = embedding_provider
        app.state.database_status = database_status
        app.state.knowledge_nodes_loaded = knowledge_nodes_loaded
        app.state.knowledge_edges_loaded = knowledge_edges_loaded
        app.state.knowledge_nodes_embedded = knowledge_nodes_embedded
        app.state.documents_loaded = documents_loaded
        app.state.document_chunks_loaded = document_chunks_loaded
        app.state.document_chunks_embedded = document_chunks_embedded
        if diagnosis_worker is not None:
            # Worker 在所有 app.state 依赖发布后再启动，避免后台 task 观察到半初始化的 runtime。
            diagnosis_worker.start()
        yield
    finally:
        # 先按角色关闭模型 HTTP 池，再释放数据库池；均不吞异常，避免测试重启后遗留资源。
        if diagnosis_worker is not None:
            # 先停止领取新任务；未完成 run 的 lease 会自然过期并由新 Worker 接管。
            await diagnosis_worker.stop()
        if auditor_runtime is not None:
            await auditor_runtime.aclose()
        if planner_runtime is not None:
            await planner_runtime.aclose()
        # 检索侧 Provider 也可能持有远程连接池与 Authorization 头；用 hasattr 而不是 isinstance，
        # 是为了让离线确定性实现和测试替身无需实现空的 aclose 就能通过同一条关闭路径。
        if reranker is not None and hasattr(reranker, "aclose"):
            await reranker.aclose()
        if hasattr(embedding_provider, "aclose"):
            await embedding_provider.aclose()
        if database_engine is not None:
            await database_engine.dispose()


app = FastAPI(
    title="DataOps Troubleshooter",
    version=__version__,
    lifespan=lifespan,
)

DEMO_ASSET_ROOT = (Path(__file__).resolve().parent.parent / "static" / "demo").resolve()
DEMO_INDEX_PATH = DEMO_ASSET_ROOT / "index.html"


@app.middleware("http")
async def enforce_api_security(request: Request, call_next):
    """在路由之前对受保护前缀执行鉴权与限流，未受保护路径零开销放行。

    用中间件而不是逐路由 `Depends` 是刻意的：前缀判定让今后新增的 `/api/v1/...` 路由默认就在
    保护内（fail closed），不会因为漏写依赖而裸奔。中间件位于 FastAPI 异常处理器之外，所以这里
    直接构造 JSONResponse；`detail` 用对象形式携带稳定 `error_code`，与 409 冲突响应保持同一
    结构，浏览器 Demo 的错误分支无需为鉴权单独写解析。守卫缺失时按 503 拒绝而不是放行，避免
    lifespan 未完成的实例把"没有守卫"当成"不需要守卫"。
    """

    if not is_protected_path(request.url.path):
        return await call_next(request)
    guard = getattr(request.app.state, "api_security", None)
    if guard is None:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error_code": "security_unavailable",
                    "message": "api security guard is not initialised",
                }
            },
        )
    rejection = guard.authorize(
        authorization_header=request.headers.get("Authorization"),
        client_host=request.client.host if request.client else None,
    )
    if rejection is not None:
        return JSONResponse(
            status_code=rejection.status_code,
            content={"detail": {"error_code": rejection.error_code, "message": rejection.message}},
            headers=rejection.headers,
        )
    return await call_next(request)


def _resolve_demo_asset(asset_name: str) -> Path:
    """解析 Demo 静态资源并执行目录边界检查。

    该同步 helper 集中处理 pathlib 文件系统操作，避免在 async 路由中阻塞式遍历路径；调用方
    只接收 demo 目录内的普通文件。返回不存在路径仍由调用方映射为明确的 404/503，而不会
    隐式回退到仓库根目录或操作系统当前目录。
    """

    candidate = (DEMO_ASSET_ROOT / asset_name).resolve()
    if DEMO_ASSET_ROOT not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="demo asset not found")
    return candidate


def _demo_index_available() -> bool:
    """同步检查入口 HTML 是否是可服务的普通文件，供异步路由读取布尔结果。

    返回值只表达资产是否存在且为普通文件；它不读取文件内容，也不把异常细节暴露给浏览器。
    单独抽成同步 helper 是为了让 Ruff 的异步阻塞检查可验证，同时让 `/demo` 缺失资源时明确
    返回 503，而不是让 FileResponse 在响应流阶段才失败。
    """

    return DEMO_INDEX_PATH.is_file()


@app.get("/demo", include_in_schema=False)
async def demo_page() -> FileResponse:
    """返回学习型单页 Demo 的静态入口，而不把前端状态混入诊断 API。

    路由只解析仓库内固定路径并返回 HTML；CSS/JavaScript 由 HTML 通过同源 `/demo/static/`
    引用。文件不存在时让 FastAPI 抛出显式错误，避免返回一张看似可用却没有交互能力的空页面。
    页面本身只调用公开资源 API，不读取服务端日志、Prompt、凭据或模型原始输出。
    """

    if not _demo_index_available():
        raise HTTPException(status_code=503, detail="diagnosis demo assets are unavailable")
    return FileResponse(DEMO_INDEX_PATH, media_type="text/html; charset=utf-8")


@app.get("/demo/static/{asset_name:path}", include_in_schema=False)
async def demo_asset(asset_name: str) -> FileResponse:
    """返回 Demo 的白名单静态资源，并阻止路径穿越访问仓库其它文件。

    ``Path.resolve`` 后必须仍位于 demo 目录内；这样即使浏览器请求 `../`，也不会把配置、
    源码或凭据路径当成静态文件。只允许文件存在且为普通文件，未知资源返回 404。
    """

    return FileResponse(_resolve_demo_asset(asset_name))


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
        documents_loaded=request.app.state.documents_loaded,
        document_chunks_loaded=request.app.state.document_chunks_loaded,
        document_chunks_embedded=request.app.state.document_chunks_embedded,
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
            document_retrieval=settings.document_retrieval_contract_id,
            run_trace=settings.run_trace_contract_id,
            api_auth=settings.api_auth_contract_id,
            run_stream=settings.run_stream_contract_id,
            model_transient_retry=settings.model_transient_retry_contract_id,
        ),
        limits=RuntimeLimits(
            max_react_steps=settings.max_react_steps,
            max_parallel_tool_actions=settings.max_parallel_tool_actions,
            react_total_timeout_seconds=settings.react_total_timeout_seconds,
            max_graph_hops=settings.max_graph_hops,
            max_audit_revisions=settings.max_audit_revisions,
            tool_retry_count=settings.tool_retry_count,
            chat_transient_retry_attempts=settings.chat_transient_retry_attempts,
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
            timeout_seconds=settings.auditor_timeout_seconds,
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
            execution_mode="postgres-worker",
            worker_status=(
                "running"
                if getattr(request.app.state, "diagnosis_worker", None) is not None
                else "disabled"
            ),
            worker_poll_seconds=settings.diagnosis_worker_poll_seconds,
            worker_lease_seconds=settings.diagnosis_worker_lease_seconds,
            worker_heartbeat_seconds=settings.diagnosis_worker_heartbeat_seconds,
            worker_max_attempts=settings.diagnosis_worker_max_attempts,
            retrieval_seed_limit=settings.diagnosis_retrieval_seed_limit,
        ),
        retrieval=RetrievalConfiguration(
            embedding_provider=settings.embedding_provider,
            embedding_dimensions=settings.embedding_dimensions,
            embedding_model=settings.embedding_model,
            embedding_endpoint_host=settings.embedding_base_url.host or "",
            rerank_provider=settings.rerank_provider,
            rerank_model=settings.rerank_model,
            rerank_endpoint_host=settings.rerank_base_url.host or "",
            rerank_candidate_multiplier=settings.rerank_candidate_multiplier,
            rerank_blend_weight=settings.rerank_blend_weight,
            score_weights=settings.hybrid_scoring_weights(),
            document_score_weights=settings.document_scoring_weights(),
            document_chunk_limit=settings.document_retrieval_chunk_limit,
            evidence_budget=settings.evidence_bundle_budget(),
        ),
        security=ApiSecurityConfiguration(
            mode=request.app.state.api_security.mode,
            contract_id=settings.api_auth_contract_id,
            protected_path_prefixes=list(PROTECTED_PATH_PREFIXES),
            rate_limit_requests=request.app.state.api_security.limiter.max_requests,
            rate_limit_window_seconds=request.app.state.api_security.limiter.window_seconds,
        ),
        stream=RunStreamConfiguration(
            contract_id=settings.run_stream_contract_id,
            poll_seconds=settings.run_stream_poll_seconds,
            keepalive_seconds=settings.run_stream_keepalive_seconds,
            max_seconds=settings.run_stream_max_seconds,
            available_under_auth=request.app.state.api_security.mode == "disabled",
        ),
    )


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    """以 Prometheus 文本格式曝光 run 状态与各层 span 耗时聚合。

    指标来自数据库聚合而不是进程内计数器，因此 API 与 Worker 分进程部署、或任一进程重启后数字都
    保持连续；这一点比"少一次查询"重要得多。诊断 runtime 未装配（无数据库）时返回 503 而不是一份
    全零文本，避免监控面板把"未部署"显示成"零错误"。
    """

    runtime = _require_diagnosis_runtime(request)
    snapshot = await runtime.get_runtime_metrics()
    return PlainTextResponse(
        render_prometheus_text(snapshot),
        media_type="text/plain; version=0.0.4; charset=utf-8",
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
    status_code=status.HTTP_202_ACCEPTED,
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
    except ActiveRunConflictError as exc:
        # 同一 session 只允许一个 queued/running run，客户端应轮询旧 run 后再提交追问。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"active_run_id": exc.active_run_id, "message": "session has an active run"},
        ) from exc
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


@app.get("/api/v1/runs/{run_id}/stream", include_in_schema=False)
async def stream_diagnosis_run(
    run_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """以 `run-stream:v1` 增量推送该 run 的状态与公开事件，未知 run 在开流前返回 404。

    推流是轮询的替代读法而不是另一条执行路径：run 仍由 Worker 执行，本路由只按游标读已落库数据，
    因此客户端断开或不支持 SSE 都不会改变任何结论。存在性检查刻意放在返回 `EventSourceResponse`
    之前——一旦响应体开始，HTTP 状态码就已发出，未知 run 只能以一条"流内错误"表达，而客户端几乎
    一定会把它当成网络抖动去重连。心跳交给 sse-starlette 的 `ping`，避免手写心跳与帧编码。

    注意浏览器 `EventSource` 无法设置请求头：`api-auth:v1` 处于 bearer 模式时这条路由会被鉴权中间件
    拒绝，前端必须退回轮询，`/health` 的 `stream.available_under_auth` 会如实报告这一点。
    """

    runtime = _require_diagnosis_runtime(request)
    # 先确认 run 存在再开流，让 404 仍然是一个真正的 HTTP 状态码而不是流内的一帧。
    if await runtime.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    settings = get_settings()
    config = RunStreamConfig(
        poll_seconds=settings.run_stream_poll_seconds,
        keepalive_seconds=settings.run_stream_keepalive_seconds,
        max_seconds=settings.run_stream_max_seconds,
    )
    cursor = resolve_stream_cursor(
        last_event_id=last_event_id,
        after_sequence=after_sequence,
    )
    return EventSourceResponse(
        iter_run_stream(runtime, run_id, after_sequence=cursor, config=config),
        ping=int(config.keepalive_seconds),
    )


@app.get("/api/v1/runs/{run_id}/trace", response_model=RunTraceResponse)
async def get_diagnosis_run_trace(run_id: str, request: Request) -> RunTraceResponse:
    """返回该 run 已落库的 span 树，用于回答"这 30 秒到底花在哪一层"。

    trace 与 run 终态写在同一事务，因此能取到 run 就一定能取到它当时的 trace；空 spans 表示该 run
    在插桩上线前执行或仍在队列中，不代表零耗时。响应只包含结构化层级、状态、耗时和 ASCII 属性，
    不含 Prompt、Thought 或供应商响应，因此可以直接给演示前端渲染火焰图。
    """

    runtime = _require_diagnosis_runtime(request)
    trace = await runtime.get_run_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    return RunTraceResponse(contract_id=RUN_TRACE_CONTRACT_ID, trace=trace)


@app.post("/api/v1/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_diagnosis_run(run_id: str, request: Request) -> RunResponse:
    """取消 queued/running run，并返回服务端最终快照。

    已取消请求幂等返回 200；completed/failed 不允许被改写，返回 409 并携带当前
    状态。这样前端可以区分“用户停止”与“系统已完成”，也不会丢失审计时间线。
    """

    runtime = _require_diagnosis_runtime(request)
    run = await runtime.cancel_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    if run.status not in {AgentRunStatus.CANCELLED} and run.status not in {
        AgentRunStatus.QUEUED,
        AgentRunStatus.RUNNING,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": run.status.value, "message": "terminal run cannot be cancelled"},
        )
    return RunResponse(contract_id=DIAGNOSIS_API_CONTRACT_ID, run=run)


@app.post(
    "/api/v1/runs/{run_id}/resume",
    response_model=MessageSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_diagnosis_run(run_id: str, request: Request) -> MessageSubmissionResponse:
    """从 cancelled run 的 session checkpoint 创建新的 queued run。

    恢复不复制旧 run 的结果或事件，也不在 HTTP 请求内执行 Planner/MCP；新 run 仍由
    PostgreSQL Worker 领取。来源不存在返回 404，来源状态不允许恢复返回 409。
    """

    runtime = _require_diagnosis_runtime(request)
    try:
        run = await runtime.resume_run(run_id)
    except RunResumeConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "run_id": exc.run_id,
                "status": exc.current_status.value,
                "message": "only cancelled runs can be resumed",
            },
        ) from exc
    except ActiveRunConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"active_run_id": exc.active_run_id, "message": "session has an active run"},
        ) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    return MessageSubmissionResponse(contract_id=DIAGNOSIS_API_CONTRACT_ID, run=run)


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


@app.delete(
    "/api/v1/memories/{memory_id}",
    response_model=MemoryDeletionResponse,
)
async def delete_memory(memory_id: str, request: Request) -> MemoryDeletionResponse:
    """永久删除案例记忆及其图关系，供用户清理错误或过期候选。

    真实删除由 memory runtime 在单一事务内完成，未知 ID 返回 404；API 不直接操作
    ORM，确保 evidence 外键级联和 GraphRAG 节点清理不会被前端绕过。
    """

    runtime = _require_memory_runtime(request)
    deleted = await runtime.delete(memory_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="case memory not found")
    request.app.state.memory_counts = await runtime.counts()
    return MemoryDeletionResponse(
        contract_id=CASE_MEMORY_CONTRACT_ID,
        memory_id=memory_id,
        deleted=True,
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
