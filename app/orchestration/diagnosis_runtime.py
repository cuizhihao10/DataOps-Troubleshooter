"""协调 GraphRAG、顶层诊断 workflow 与 PostgreSQL session/run/event 资源持久化。

资源 runtime 只负责入队、读取和在 Worker 租约内执行；长耗时 GraphRAG、模型和 MCP I/O 不占用
HTTP 请求或数据库事务。终态、公开事件和版本化 session checkpoint 仍原子提交，失败不覆盖上一快照。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory.checkpoint import (
    SessionCheckpoint,
    build_checkpoint_retrieval_query,
    build_session_checkpoint,
    restore_agent_state,
)
from app.observability.metrics import RuntimeMetricsSnapshot
from app.observability.tracing import (
    RunTrace,
    RunTraceCollector,
    TraceSpanKind,
    bind_run_trace_collector,
    reset_run_trace_collector,
    trace_span,
)
from app.orchestration.diagnosis_models import DiagnosisRunRequest, DiagnosisRunResult
from app.orchestration.models import ReactStopReason
from app.orchestration.run_models import (
    DIAGNOSIS_API_CONTRACT_ID,
    AgentRunSnapshot,
    AgentRunStatus,
    ClaimedDiagnosisRun,
    DiagnosisMessage,
    DiagnosisSession,
    RunEventList,
    RunEventPhase,
    RunPublicEvent,
    RunResumeConflictError,
)
from app.persistence.run_repository import PostgresDiagnosisRunRepository
from app.retrieval.budget import build_evidence_bundle
from app.retrieval.document_repository import PostgresDocumentRepository
from app.retrieval.document_service import DocumentRetrievalService
from app.retrieval.documents import DocumentRetrievalResult, DocumentScoringWeights
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.models import EvidenceBundleBudget, GraphEvidenceBundle, HybridScoringWeights
from app.retrieval.repository import PostgresGraphRepository
from app.retrieval.reranker import RerankerProvider
from app.retrieval.service import GraphRetrievalService


class DiagnosisWorkflow(Protocol):
    """声明资源 runtime 调用顶层审计诊断图所需的最小异步接口。

    生产 ``AuditedDiagnosisWorkflow`` 和测试替身都返回完整 DiagnosisRunResult；协议不暴露子图节点，
    使 API runtime 不能绕过 Planner、Auditor 或 memory staging 顺序。
    """

    async def run(self, request: DiagnosisRunRequest) -> DiagnosisRunResult:
        """执行一个已分配 run/session ID 的完整诊断并返回强类型终态。

        实现失败必须抛异常；资源 runtime 会保存安全 failed 状态并通过异常链保留原始错误，而不是
        构造部分 DiagnosisRunResult。
        """

        ...


class DiagnosisContextRetriever(Protocol):
    """声明 message 提交前构造预算化 GraphRAG Evidence Bundle 的只读接口。

    生产实现使用 PostgreSQL/pgvector，测试可注入确定性 Bundle；异常由资源 runtime 标记为 run
    失败，不能被转换为 null 伪装“未执行检索”。
    """

    async def retrieve(self, query: str) -> GraphEvidenceBundle:
        """根据非空用户问题返回版本化、受预算限制的图证据上下文。

        返回对象可为空节点/路径但必须是合法 GraphEvidenceBundle；Provider、SQL 或预算构造错误
        继续抛出，调用方不会自行生成知识事实。
        """

        ...


class DiagnosisExecutionFailed(RuntimeError):
    """表示 run 已持久化为 failed，HTTP 层可安全返回 run_id 和稳定错误码。

    原始异常通过 ``raise ... from exc`` 保留给日志/调试，不复制到公开字符串；调用者可随后 GET
    run/events 查看净化失败信息，而不会暴露数据库 URL、模型响应体或凭据。
    """

    def __init__(self, *, run_id: str, error_code: str, public_message: str) -> None:
        """保存 run ID、稳定分类和公开摘要并初始化标准 RuntimeError。

        三个字段由 runtime 固定/净化；异常消息只使用 public_message，不接收底层异常文本。空值由
        构造调用点避免，API 无需解析自由文本即可构造错误响应。
        """

        super().__init__(public_message)
        self.run_id = run_id
        self.error_code = error_code
        self.public_message = public_message


class PostgresGraphContextRetriever:
    """为每次 message 打开短会话，执行混合 GraphRAG 与文档 RAG 并构造预算化 Evidence Bundle。

    Provider/权重/预算为进程级不可变依赖，AsyncSession 每次调用独占；查询只读且不 commit，退出
    上下文立即归还连接。该对象不调用 Planner 或写 run 表。图通道与文档通道共用同一次会话顺序执行，
    因为 AsyncSession 不能并发复用；两者的失败语义也一致——任何一侧异常都向上抛出，绝不用空结果
    伪装成"知识库里没有这类证据"。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        *,
        score_weights: HybridScoringWeights,
        budget: EvidenceBundleBudget,
        seed_limit: int,
        max_hops: int,
        reranker: RerankerProvider | None = None,
        rerank_candidate_multiplier: int = 3,
        rerank_blend_weight: float = 0.4,
        document_score_weights: DocumentScoringWeights | None = None,
        document_chunk_limit: int = 4,
    ) -> None:
        """保存会话工厂、向量 Provider、可选 cross-encoder 重排器和集中检索/上下文预算。

        seed_limit 限制 1..20，max_hops 限制产品批准的 1..2；构造不连接数据库或生成向量。其余
        Pydantic 配置已验证权重和字节/节点/路径预算。重排器为可选依赖：为 None 时检索退回纯一阶段
        排序，因此没有远程凭据的离线演示仍能完整跑通 GraphRAG，而不是在诊断中途才失败。
        `document_score_weights` 为 None 表示本进程未启用文档通道（例如尚未导入语料），此时 Bundle
        里不会出现 `selected_documents`，而不是返回一批空壳切片让报告以为有出处可引。
        """

        if not 1 <= seed_limit <= 20:
            raise ValueError("diagnosis retrieval seed limit must be between 1 and 20")
        if not 1 <= max_hops <= 2:
            raise ValueError("diagnosis graph max_hops must be between 1 and 2")
        if not 1 <= document_chunk_limit <= 20:
            raise ValueError("document retrieval chunk limit must be between 1 and 20")
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._score_weights = score_weights
        self._budget = budget
        self._seed_limit = seed_limit
        self._max_hops = max_hops
        self._reranker = reranker
        self._rerank_candidate_multiplier = rerank_candidate_multiplier
        self._rerank_blend_weight = rerank_blend_weight
        self._document_score_weights = document_score_weights
        self._document_chunk_limit = document_chunk_limit

    async def retrieve(self, query: str) -> GraphEvidenceBundle:
        """执行全文/向量种子、可选精排、白名单图扩展、文档召回和原子预算选择并返回 Bundle。

        query 为空由 GraphRetrievalService 显式失败；同一 AsyncSession 顺序执行 SQL，避免并发复用。
        离开只读会话后 ORM 数据已转换为领域模型，返回值不依赖打开连接。文档检索排在图检索之后但
        共享同一次会话与同一份查询文本，使两条通道的召回可在同一次 run 事件里逐项对照。
        """

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("diagnosis retrieval query must not be blank")
        # AgentState 保留最多 4000 字符原问题；GraphRAG v3 查询契约上限为 2000，检索侧显式截断。
        retrieval_query = normalized_query[:2000]
        documents: DocumentRetrievalResult | None = None
        async with self._session_factory() as session:
            service = GraphRetrievalService(
                PostgresGraphRepository(session),
                self._embedding_provider,
                score_weights=self._score_weights,
                reranker=self._reranker,
                rerank_candidate_multiplier=self._rerank_candidate_multiplier,
                rerank_blend_weight=self._rerank_blend_weight,
            )
            with trace_span(
                TraceSpanKind.RETRIEVAL,
                "retrieval.graph_channel",
                seed_limit=self._seed_limit,
                max_hops=self._max_hops,
            ) as span:
                result = await service.retrieve(
                    retrieval_query,
                    seed_limit=self._seed_limit,
                    max_hops=self._max_hops,
                )
                span.annotate(
                    retrieval_mode=result.mode.value,
                    candidate_count=result.candidate_count,
                    seed_count=len(result.seeds),
                    path_count=len(result.paths),
                    reranker_model=result.reranker_model or "none",
                )
            if self._document_score_weights is not None:
                document_service = DocumentRetrievalService(
                    PostgresDocumentRepository(session),
                    self._embedding_provider,
                    score_weights=self._document_score_weights,
                    reranker=self._reranker,
                    rerank_candidate_multiplier=self._rerank_candidate_multiplier,
                    rerank_blend_weight=self._rerank_blend_weight,
                )
                # 两条通道分别开 span：文档 RAG 与图召回共享 embedding/精排服务，只有分开计时才能
                # 回答"哪条通道值得继续投入预算"，合并成一个 retrieval span 会永久掩盖这个问题。
                with trace_span(
                    TraceSpanKind.RETRIEVAL,
                    "retrieval.document_channel",
                    chunk_limit=self._document_chunk_limit,
                ) as span:
                    documents = await document_service.retrieve(
                        retrieval_query,
                        chunk_limit=self._document_chunk_limit,
                    )
                    span.annotate(
                        candidate_count=documents.candidate_count,
                        chunk_count=len(documents.chunks),
                        reranker_model=documents.reranker_model or "none",
                    )
        return build_evidence_bundle(result, budget=self._budget, documents=documents)


class DiagnosisApplicationRuntime:
    """实现 session 创建、同步 message 执行、run 查询和公开事件读取。

    runtime 只保存 session factory、检索器、顶层 workflow、时钟和 ID 工厂；每个数据库写阶段使用
    独立短事务。模型/MCP 执行期间不持有 run 行锁，避免长事务占用连接和阻塞轮询。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retriever: DiagnosisContextRetriever,
        workflow: DiagnosisWorkflow,
        now_factory: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        """注入持久化、检索、工作流及可测试的 UTC 时钟/ID 工厂。

        构造不执行 I/O；默认 ID 为前缀加 16 位随机十六进制，默认时间为 UTC。测试可注入确定序列，
        但工厂返回值仍会在 Pydantic/数据库主键边界校验。
        """

        self._session_factory = session_factory
        self._retriever = retriever
        self._workflow = workflow
        self._now_factory = now_factory or _utc_now
        self._id_factory = id_factory or _random_id

    async def create_session(self, *, title: str) -> DiagnosisSession:
        """创建并提交一个空诊断会话，返回可直接用于 API 的资源快照。

        title 先去除首尾空白，纯空显式失败；单事务插入确保响应前资源已持久化。ID 冲突或数据库
        故障原样传播，API 不返回未提交 session。
        """

        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("diagnosis session title must not be blank")
        now = self._now_factory()
        async with self._session_factory.begin() as session:
            repository = PostgresDiagnosisRunRepository(session)
            return await repository.create_session(
                session_id=self._id_factory("session"),
                title=normalized_title,
                now=now,
            )

    async def submit_message(
        self,
        session_id: str,
        message: DiagnosisMessage,
    ) -> AgentRunSnapshot | None:
        """创建 queued run 并立即返回，绝不在 HTTP 请求内执行 GraphRAG、模型或 MCP。

        会话不存在返回 None；同 session 已有 queued/running run 时仓储抛 ActiveRunConflictError。
        PostgreSQL 事务只写入用户输入和队列状态，Worker 随后通过租约领取并调用
        ``execute_claimed_run``，因此客户端可以用 GET run/events 轮询而不会占用请求连接。
        """

        run_id = self._id_factory("run")
        queued_at = self._now_factory()
        async with self._session_factory.begin() as session:
            repository = PostgresDiagnosisRunRepository(session)
            queued = await repository.create_run(
                run_id=run_id,
                session_id=session_id,
                message=message,
                now=queued_at,
            )
        return queued

    async def cancel_run(
        self,
        run_id: str,
        *,
        reason: str = "user_requested",
    ) -> AgentRunSnapshot | None:
        """取消一个尚未完成的 run，并以事务快照形式返回结果。

        取消只改变 run 状态与公开系统事件，不删除 session checkpoint 或历史记忆；
        因而用户可以稍后通过 resume 创建新的 queued run。Repository 的行锁保证
        它与 Worker 的完成提交互斥，重复请求返回同一 cancelled 快照。
        """

        now = self._now_factory()
        async with self._session_factory.begin() as session:
            return await PostgresDiagnosisRunRepository(session).cancel_run(
                run_id,
                now=now,
                reason=reason,
            )

    async def resume_run(self, run_id: str) -> AgentRunSnapshot | None:
        """从 cancelled run 的原始输入创建一个新的 queued run。

        这是可靠的 run-level resume：它复用同一 session 的最新 checkpoint，而不是
        声称实现 LangGraph 内部节点级恢复。只有 cancelled 来源可恢复；completed、
        failed、queued 或 running 都返回明确冲突，避免用户误触发重复诊断。
        """

        new_run_id = self._id_factory("run")
        now = self._now_factory()
        async with self._session_factory.begin() as session:
            repository = PostgresDiagnosisRunRepository(session)
            source = await repository.lock_run(run_id)
            if source is None:
                return None
            if source.status is not AgentRunStatus.CANCELLED:
                raise RunResumeConflictError(run_id, source.status)
            return await repository.create_run(
                run_id=new_run_id,
                session_id=source.session_id,
                message=DiagnosisMessage(
                    content=source.user_query,
                    intent=source.intent,
                    components=source.components,
                    history_trigger=source.history_trigger,
                ),
                now=now,
            )

    async def execute_claimed_run(self, claim: ClaimedDiagnosisRun) -> AgentRunSnapshot:
        """在 Worker 已取得有效租约后执行完整 workflow，并原子提交终态/事件/checkpoint/trace。

        领取事务与外部 I/O 分离；执行开始时重新读取 checkpoint，随后 GraphRAG、LangGraph 和 MCP
        都在事务外运行。成功或失败最终事务都带 worker_id，过期/被接管的旧 Worker 无法覆盖新结果。
        整个执行体绑定一个 ``RunTraceCollector``：检索、workflow 与其内部所有插桩点通过 ContextVar
        自动挂到同一棵树上，因此无需把 span 参数逐层传进 LangGraph 节点。
        """

        run_id = claim.run.run_id
        message = claim.message()
        collector = RunTraceCollector(run_id)
        trace_token = bind_run_trace_collector(collector)
        try:
            return await self._execute_traced_run(claim, collector, message)
        finally:
            # 采集器必须在 Worker 处理下一个 run 之前解绑，否则上一次的 span 会写进新 run 的 trace。
            reset_run_trace_collector(trace_token)

    async def _execute_traced_run(
        self,
        claim: ClaimedDiagnosisRun,
        collector: RunTraceCollector,
        message: DiagnosisMessage,
    ) -> AgentRunSnapshot:
        """在已绑定采集器的上下文中执行诊断，并在根 span 关闭后写入终态与 trace。

        根 span 刻意在最终事务之前关闭：span 只在结束时才拿到耗时，若把提交也包进根 span，快照就
        必然缺少根节点本身。代价是最后一次数据库提交的耗时不出现在 trace 里，换来的是 trace 与 run
        终态严格原子——不会出现"run 已 completed 但 trace 缺失"这种事后无法解释的状态。
        """

        run_id = claim.run.run_id
        session_id = claim.run.session_id
        async with self._session_factory() as session:
            checkpoint = await PostgresDiagnosisRunRepository(session).get_checkpoint(session_id)

        try:
            with trace_span(
                TraceSpanKind.WORKFLOW,
                "diagnosis.run",
                intent=message.intent.value,
                history_trigger=message.history_trigger.value,
                attempt=claim.run.attempt_count,
                resumed=checkpoint is not None,
            ) as root:
                # 当前追问排在检索查询首位，上一轮公开报告只负责补全省略主题，不注入隐藏模型输出。
                retrieval_query = build_checkpoint_retrieval_query(
                    message.content,
                    checkpoint,
                )
                with trace_span(TraceSpanKind.RETRIEVAL, "retrieval.evidence_bundle") as span:
                    evidence_bundle = await self._retriever.retrieve(retrieval_query)
                    span.annotate(
                        retrieval_mode=evidence_bundle.retrieval_mode.value,
                        node_count=len(evidence_bundle.selected_nodes),
                        path_count=len(evidence_bundle.selected_paths),
                        document_count=len(evidence_bundle.selected_documents),
                        used_bytes=evidence_bundle.used_bytes,
                    )
                initial_state = restore_agent_state(
                    checkpoint,
                    run_id=run_id,
                    session_id=session_id,
                    user_query=message.content,
                )
                with trace_span(
                    TraceSpanKind.WORKFLOW, "workflow.audited_diagnosis"
                ) as workflow_span:
                    result = await self._workflow.run(
                        DiagnosisRunRequest(
                            state=initial_state,
                            capability_request=message.capability_request(),
                            evidence_bundle=evidence_bundle,
                        )
                    )
                    workflow_span.annotate(
                        audit_status=result.report.outcome.value,
                        react_steps=result.react.state.react_step,
                        tool_events=len(result.react.state.tool_events),
                        recalled_memories=len(result.recalled_memories),
                    )
                report = result.report.state.draft_report
                root.annotate(
                    stop_reason=_span_stop_reason(result.react.state.stop_reason),
                    root_cause_count=(0 if report is None else len(report.root_causes)),
                )
            completed_at = self._now_factory()
            next_checkpoint = build_session_checkpoint(
                result,
                checkpoint_version=(
                    1 if checkpoint is None else checkpoint.checkpoint_version + 1
                ),
                created_at=(completed_at if checkpoint is None else checkpoint.created_at),
                updated_at=completed_at,
            )
            events = _project_run_events(
                run_id,
                evidence_bundle=evidence_bundle,
                result=result,
                restored_checkpoint=checkpoint,
                saved_checkpoint=next_checkpoint,
                created_at=completed_at,
            )
            async with self._session_factory.begin() as session:
                repository = PostgresDiagnosisRunRepository(session)
                return await repository.complete_run(
                    run_id,
                    result=result,
                    events=events,
                    checkpoint=next_checkpoint,
                    worker_id=claim.worker_id,
                    now=completed_at,
                    trace=collector.snapshot(),
                )
        except Exception as exc:
            # 用户取消可能在 GraphRAG/MCP I/O 期间发生；完成提交会因 lease/state
            # 不再属于 Worker 而失败。此时读取公开快照并正常返回 cancelled，避免
            # 把预期控制流记录成 diagnosis_execution_failed。
            current = await self.get_run(run_id)
            if current is not None and current.status is AgentRunStatus.CANCELLED:
                return current
            failed_at = self._now_factory()
            public_message = "诊断执行失败；请使用 run_id 查询安全失败事件。"
            failure = RunPublicEvent(
                event_id=_event_id(run_id, 1),
                run_id=run_id,
                sequence=1,
                phase=RunEventPhase.SYSTEM,
                event_type="diagnosis_execution_failed",
                summary=public_message,
                payload={"error_code": "diagnosis_execution_failed"},
                created_at=failed_at,
            )
            # 失败持久化使用新事务；若该事务也失败，数据库异常应替代安全包装向上暴露，而非吞掉。
            try:
                async with self._session_factory.begin() as session:
                    repository = PostgresDiagnosisRunRepository(session)
                    await repository.fail_run(
                        run_id,
                        error_code="diagnosis_execution_failed",
                        error_message=public_message,
                        event=failure,
                        worker_id=claim.worker_id,
                        now=failed_at,
                        trace=collector.snapshot(),
                    )
            except LookupError:
                # Worker 失去 lease 或用户先取消时，失败写入会被并发保护拒绝；
                # 只有确认当前状态为 cancelled 才吞掉该异常，其余情况继续暴露。
                current = await self.get_run(run_id)
                if current is not None and current.status is AgentRunStatus.CANCELLED:
                    return current
                raise
            raise DiagnosisExecutionFailed(
                run_id=run_id,
                error_code="diagnosis_execution_failed",
                public_message=public_message,
            ) from exc

    async def get_run_trace(self, run_id: str) -> RunTrace | None:
        """在短只读会话中返回一次 run 的持久化调用链，未知 run 返回 None。

        与事件时间线分开读取，因为 trace 的体量随插桩密度增长，把它塞进 GET run 会让轮询响应无界
        变大；调用方通常只在诊断结束后打开一次火焰图。
        """

        async with self._session_factory() as session:
            return await PostgresDiagnosisRunRepository(session).list_trace_spans(run_id)

    async def get_runtime_metrics(self) -> RuntimeMetricsSnapshot:
        """在短只读会话中聚合 run 状态与各层 span 耗时，供 /metrics 曝光。

        每次抓取都重新聚合而不缓存：抓取间隔通常在 15 秒以上，两条带索引的 GROUP BY 成本远低于
        维护一份可能与数据库不一致的缓存，而"指标比真实状态旧"恰恰是排障时最误导人的情况。
        """

        async with self._session_factory() as session:
            return await PostgresDiagnosisRunRepository(session).aggregate_metrics()

    async def get_run(self, run_id: str) -> AgentRunSnapshot | None:
        """在短只读会话中返回 run 快照，未知 ID 返回 None。

        JSONB 结果会重新通过 Pydantic；数据库或契约错误显式传播。方法不加载事件，避免 GET run
        响应随时间线长度无界增长。
        """

        async with self._session_factory() as session:
            return await PostgresDiagnosisRunRepository(session).get_run(run_id)

    async def get_events(self, run_id: str) -> RunEventList | None:
        """读取并验证一个 run 的连续公开事件列表，未知 ID 返回 None。

        仓储负责 SQL 排序，RunEventList 再检查同 run 和 1..N 连续性；响应带版本化资源契约，前端
        无需猜测事件 Schema。
        """

        async with self._session_factory() as session:
            events = await PostgresDiagnosisRunRepository(session).list_events(run_id)
        if events is None:
            return None
        return RunEventList(
            contract_id=DIAGNOSIS_API_CONTRACT_ID,
            run_id=run_id,
            events=events,
        )

    async def get_events_after(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> tuple[RunPublicEvent, ...] | None:
        """在短只读会话中返回序号大于游标的公开事件，未知 run 返回 None。

        SSE 推流按游标续传，需要的是增量切片而不是 `RunEventList`（后者要求序号覆盖 1..N）。
        每次轮询单独开短会话而不是长期持有一个事务，这样一条挂了几分钟的浏览器连接不会占住
        数据库连接池，也不会让快照读看不到 Worker 后续提交的事件。
        """

        async with self._session_factory() as session:
            return await PostgresDiagnosisRunRepository(session).list_events_after(
                run_id,
                after_sequence=after_sequence,
            )


def _project_run_events(
    run_id: str,
    *,
    evidence_bundle: GraphEvidenceBundle,
    result: DiagnosisRunResult,
    restored_checkpoint: SessionCheckpoint | None,
    saved_checkpoint: SessionCheckpoint,
    created_at: datetime,
) -> tuple[RunPublicEvent, ...]:
    """把检索、ReAct、报告、记忆和 checkpoint 投影为连续安全公开时间线。

    检索先占 sequence=1 并记录恢复来源，随后保持两个子图事件原顺序，最后追加 memory 与快照
    保存事件。payload 只选有限枚举、ID、计数和布尔值，不序列化 AgentState、Prompt 或 embedding。
    """

    event_data: list[tuple[RunEventPhase, str, str, dict[str, object]]] = [
        (
            RunEventPhase.RETRIEVAL,
            "graphrag_context_retrieved",
            (
                f"GraphRAG 上下文包含 {len(evidence_bundle.selected_nodes)} 个节点、"
                f"{len(evidence_bundle.selected_paths)} 条完整路径和 "
                f"{len(evidence_bundle.selected_documents)} 个文档片段。"
            ),
            {
                "retrieval_mode": evidence_bundle.retrieval_mode.value,
                "selected_node_ids": [item.node_id for item in evidence_bundle.selected_nodes],
                "selected_path_ids": [item.path_id for item in evidence_bundle.selected_paths],
                "selected_chunk_ids": [
                    item.chunk_id for item in evidence_bundle.selected_documents
                ],
                "selected_document_ids": _stable_document_ids(evidence_bundle),
                "truncated": evidence_bundle.truncated,
                "history_match_count": len(result.history_case_matches),
                "history_case_ids": [item.case_id for item in result.history_case_matches],
                "restored_checkpoint_version": (
                    restored_checkpoint.checkpoint_version
                    if restored_checkpoint is not None
                    else None
                ),
                "restored_from_run_id": (
                    restored_checkpoint.source_run_id
                    if restored_checkpoint is not None
                    else None
                ),
            },
        )
    ]
    for event in result.react.events:
        event_data.append(
            (
                RunEventPhase.REACT,
                event.event_type.value,
                event.summary,
                {
                    "source_event_id": event.event_id,
                    "tool_name": event.tool_name.value if event.tool_name is not None else None,
                    # 批次大小必须落进 run_events，否则回放时间线时无法区分"三步串行"和
                    # "一批三个并行"，而这正是解释 P95 延迟改善的唯一依据。
                    "parallel_action_count": event.parallel_action_count,
                    "observation_refs": list(event.observation_refs),
                    "stop_reason": event.stop_reason,
                },
            )
        )
    for event in result.report.events:
        event_data.append(
            (
                RunEventPhase.REPORT,
                event.event_type.value,
                event.summary,
                {
                    "source_event_id": event.event_id,
                    "audit_status": (
                        event.audit_status.value if event.audit_status is not None else None
                    ),
                    "issue_codes": [item.value for item in event.issue_codes],
                    "revision_number": event.revision_number,
                },
            )
        )
    stage = result.memory_stage
    event_data.append(
        (
            RunEventPhase.MEMORY,
            "case_memory_staged",
            f"长期记忆处理结果为 {stage.status.value}。",
            {
                "status": stage.status.value,
                "memory_id": stage.memory.memory_id if stage.memory is not None else None,
                "duplicate_type": stage.duplicate_type.value,
                "similarity": stage.similarity,
            },
        )
    )
    event_data.append(
        (
            RunEventPhase.SYSTEM,
            "session_checkpoint_saved",
            f"会话短期状态已保存为 checkpoint v{saved_checkpoint.checkpoint_version}。",
            {
                "checkpoint_contract_id": saved_checkpoint.contract_id,
                "checkpoint_version": saved_checkpoint.checkpoint_version,
                "source_run_id": saved_checkpoint.source_run_id,
            },
        )
    )
    return tuple(
        RunPublicEvent(
            event_id=_event_id(run_id, sequence),
            run_id=run_id,
            sequence=sequence,
            phase=phase,
            event_type=event_type,
            summary=summary,
            payload=payload,
            created_at=created_at,
        )
        for sequence, (phase, event_type, summary, payload) in enumerate(event_data, start=1)
    )


def _span_stop_reason(stop_reason: str | None) -> str:
    """把 ReAct 终止原因压成 span 属性可接受的稳定 ASCII 分类。

    `state.stop_reason` 有两个来源：控制器门禁产生的 `ReactStopReason` 枚举值（ASCII 标识符），
    以及 Planner 在 finish/need_user_input 时自述的自由文本理由（实测多为中文整句）。span 属性
    被结构性限制为 ASCII 标识符，正是为了让 Prompt/Thought 之类的自由文本无法进入遥测出口，
    因此这里只逐字保留可穷举的控制器原因，模型自述理由统一归类为 `planner_reported`，完整措辞
    仍通过 run_events 的公开摘要呈现。

    这个函数的存在本身是一次实测教训：直接把原值传给 `annotate` 会在模型正常结束时抛
    ValueError，被外层捕获后写成 `diagnosis_execution_failed`——可观测性反过来把成功的诊断判成
    失败。遥测永远不该改变业务终态，所以宁可在写入前降低分辨率，也不放宽属性白名单。
    """

    if stop_reason is None:
        return "unspecified"
    try:
        return ReactStopReason(stop_reason).value
    except ValueError:
        return "planner_reported"


def _stable_document_ids(evidence_bundle: GraphEvidenceBundle) -> list[str]:
    """按切片命中顺序返回去重后的文档 ID 列表，用于公开检索事件。

    事件里同时给出切片 ID 和文档 ID：切片 ID 用于核对报告脚注，文档 ID 用于让人一眼看出这次诊断
    参考了哪几份资料。保持首次命中顺序而不排序，因为顺序本身携带"哪份文档最相关"的检索信息。
    """

    document_ids: list[str] = []
    for chunk in evidence_bundle.selected_documents:
        if chunk.doc_id not in document_ids:
            document_ids.append(chunk.doc_id)
    return document_ids


def _event_id(run_id: str, sequence: int) -> str:
    """根据 run ID 和 sequence 生成稳定 16 位事件摘要 ID。

    同一结果重投影会得到相同 ID，数据库 run/sequence 唯一约束仍是最终防线；SHA-256 只用于稳定
    标识，不承担安全签名或凭据散列职责。
    """

    digest = sha256(f"{run_id}|{sequence}".encode()).hexdigest()[:16]
    return f"run_evt_{digest}"


def _random_id(prefix: str) -> str:
    """生成符合 session/run Pydantic pattern 的随机十六进制资源 ID。

    prefix 仅允许 runtime 内部传入 session/run；UUID4 不包含业务数据，避免在公开 ID 中泄露用户
    查询、组件或数据库序列。主键冲突仍由数据库显式失败。
    """

    if prefix not in {"session", "run"}:
        raise ValueError("diagnosis resource ID prefix must be session or run")
    return f"{prefix}_{uuid4().hex[:16]}"


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，供资源创建、完成和事件持久化。

    独立函数允许测试注入序列时钟，不读取本地时区或生成 naive datetime；数据库仍以 timestamptz
    保存并在读取时再次校验。
    """

    return datetime.now(UTC)
