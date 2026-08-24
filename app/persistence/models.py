"""知识图与受控长期案例记忆的 SQLAlchemy 表映射。

表级约束重复验证领域枚举、向量空间、状态和计数，形成数据库最后一道防线。知识图与案例记忆
共享 PostgreSQL/pgvector，但保持独立表和仓储职责。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """为本项目 ORM 映射提供统一 SQLAlchemy metadata 注册中心。

    Alembic 从 `Base.metadata` 发现表结构，运行时仓储则使用具体 Record；基类不承载领域行为，
    从而保持 Pydantic 领域模型与数据库持久化模型分离。

    `eager_defaults=True` 是异步栈的正确性要求而不是性能开关：带 `server_default` / `onupdate`
    的列在 flush 之后会被标记为过期，下一次读取会触发一次隐式 SELECT。同步代码里这只是多一次
    查询，但在 asyncio 下属性读取无法 await，SQLAlchemy 只能抛 `MissingGreenlet`，于是"先写后读
    同一行"的仓储方法（例如领取 run 后立刻把 ORM 行转成公开快照）会在真实 PostgreSQL 上直接崩。
    开启后 PostgreSQL 用 RETURNING 在同一条 INSERT/UPDATE 里取回生成值，属性永不过期，也不会多
    一次往返。

    与之配套，所有 `updated_at` 只保留 `server_default` 而**不使用** `onupdate=func.now()`：写入方
    全部显式传入注入的 `now`，而 `onupdate` 只在该列没有净变化时才触发，届时数据库事务时钟会悄悄
    覆盖应用时间戳。这既让"租约必须晚于本次更新"这类跨字段校验随机失败，也让 checkpoint 的
    `updated_at` 一致性比对无法复现。审计时间戳的真相应由注入时钟决定，不由数据库时钟决定。
    """

    __mapper_args__ = {"eager_defaults": True}


class KnowledgeNodeRecord(Base):
    """把知识图节点映射到 PostgreSQL，并在数据库层重复关键完整性约束。

    JSONB 保存别名，GIN 表达式索引由迁移创建，Vector 可空字段为后续语义召回预留。类型与可靠性
    CheckConstraint 防止绕过 Pydantic 的其他写入者污染表；Record 只负责持久化，不生成证据结论。
    """

    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        CheckConstraint(
            "node_type IN "
            "('component','task','dataset','symptom','root_cause','solution','case','sop')",
            name="ck_knowledge_nodes_type",
        ),
        CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_knowledge_nodes_reliability",
        ),
        CheckConstraint(
            "(embedding IS NULL AND embedding_provider IS NULL AND "
            "embedding_dimensions IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_provider IS NOT NULL AND "
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions)",
            name="ck_knowledge_nodes_embedding_metadata",
        ),
        Index("ix_knowledge_nodes_type", "node_type"),
        Index("ix_knowledge_nodes_source", "source_id"),
        Index(
            "ix_knowledge_nodes_embedding_space",
            "embedding_provider",
            "embedding_dimensions",
        ),
    )

    node_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_span: Mapped[str] = mapped_column(Text, nullable=False)
    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class KnowledgeEdgeRecord(Base):
    """把有向知识关系映射到 PostgreSQL，并禁止非法类型、权重、自环和重复来源边。

    两端外键使用级联删除保证删节点不留悬空边；唯一约束允许不同来源描述同一关系，同时阻止同一
    来源重复写入。边权用于路径评分而非概率证明，最终结论仍需来源与实时 Observation 支撑。
    """

    __tablename__ = "knowledge_edges"
    __table_args__ = (
        CheckConstraint(
            "relation_type IN "
            "('RUNS_ON','DEPENDS_ON','PRODUCES','CONSUMES','MANIFESTS_AS',"
            "'CAUSED_BY','RESOLVED_BY','SIMILAR_TO')",
            name="ck_knowledge_edges_relation_type",
        ),
        CheckConstraint(
            "weight > 0 AND weight <= 1",
            name="ck_knowledge_edges_weight",
        ),
        CheckConstraint(
            "from_node_id <> to_node_id",
            name="ck_knowledge_edges_no_self_loop",
        ),
        UniqueConstraint(
            "from_node_id",
            "to_node_id",
            "relation_type",
            "source_id",
            name="uq_knowledge_edges_source_relation",
        ),
        Index("ix_knowledge_edges_from", "from_node_id"),
        Index("ix_knowledge_edges_to", "to_node_id"),
        Index("ix_knowledge_edges_relation", "relation_type"),
    )

    edge_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    from_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    to_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_span: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DocumentRecord(Base):
    """把运维文档（Runbook/SOP/复盘/FAQ）的元数据映射到 PostgreSQL。

    文档本身不参与检索，只承载切片共享的类型、权威度与来源标识：`reliability` 直接充当文档检索
    的 authority 因子，因此这里用 CheckConstraint 兜住取值区间，避免手工写库的 1.5 让融合分数越界。
    正文一律存在 `document_chunks`，本表只保留可在报告脚注中安全展示的公开字段。
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('runbook','sop','postmortem','faq')",
            name="ck_documents_type",
        ),
        CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_documents_reliability",
        ),
        CheckConstraint(
            "jsonb_typeof(components) = 'array' AND jsonb_array_length(components) >= 1",
            name="ck_documents_components",
        ),
        Index("ix_documents_type", "doc_type"),
        Index("ix_documents_source", "source_id"),
    )

    doc_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    components: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[str] = mapped_column(String(50), nullable=False)
    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DocumentChunkRecord(Base):
    """把确定性切分后的文档片段与其向量映射到 PostgreSQL，作为文档 RAG 的唯一检索单元。

    `(doc_id, ordinal)` 唯一约束让"同一文档内切片连续且不重复"在数据库层成立，重新导入同一文档时
    仓储先删后插即可保持序号语义；embedding 三元组沿用知识节点的全有或全无约束，使 128 维基线与
    1024 维 bge-m3 向量不会混进同一次 cosine 比较。GIN 全文索引由迁移用表达式创建。
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_document_chunks_ordinal"),
        CheckConstraint("char_count >= 1", name="ck_document_chunks_char_count"),
        CheckConstraint(
            "(embedding IS NULL AND embedding_provider IS NULL AND "
            "embedding_dimensions IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_provider IS NOT NULL AND "
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions)",
            name="ck_document_chunks_embedding_metadata",
        ),
        UniqueConstraint("doc_id", "ordinal", name="uq_document_chunks_doc_ordinal"),
        Index("ix_document_chunks_doc", "doc_id"),
        Index(
            "ix_document_chunks_embedding_space",
            "embedding_provider",
            "embedding_dimensions",
        ),
    )

    chunk_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CaseMemoryRecord(Base):
    """把结构化案例、确认状态、去重签名和 embedding 映射到 PostgreSQL。

    JSONB 保存有序列表字段，Vector 保存不进入领域/API 的检索向量；签名唯一约束提供精确去重，
    状态/计数/向量 CheckConstraint 防止绕过 Service 的直接写入污染默认 confirmed 召回。
    """

    __tablename__ = "case_memories"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','confirmed','rejected')",
            name="ck_case_memories_status",
        ),
        CheckConstraint(
            "occurrence_count >= 1",
            name="ck_case_memories_occurrence_count",
        ),
        CheckConstraint(
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions",
            name="ck_case_memories_embedding_dimensions",
        ),
        UniqueConstraint("signature", name="uq_case_memories_signature"),
        Index("ix_case_memories_status", "status"),
        Index("ix_case_memories_embedding_space", "embedding_provider", "embedding_dimensions"),
    )

    memory_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    symptoms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    fault_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    solution_steps: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    components: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class MemoryEvidenceRecord(Base):
    """保存案例与每次来源 run 的 Evidence 引用关联，支持幂等 occurrence 统计。

    三列复合主键允许不同运行引用同名证据，同时阻止同一运行重复插入同一引用；source_run_id
    让 Service 判断重放是否已经计数。删除案例时级联清理关联，不遗留不可追溯证据。
    """

    __tablename__ = "memory_evidence"
    __table_args__ = (Index("ix_memory_evidence_source_run", "source_run_id"),)

    memory_id: Mapped[str] = mapped_column(
        ForeignKey("case_memories.memory_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_ref: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DiagnosisSessionRecord(Base):
    """映射资源化诊断会话及最后一次用户问题摘要。

    会话只保存公开标题和截断摘要，不复制完整 Prompt 或模型输出；updated_at 在每次创建 run 时刷新，
    便于列表按最近活动排序。具体 run 通过外键表关联。
    """

    __tablename__ = "diagnosis_sessions"
    __table_args__ = (Index("ix_diagnosis_sessions_updated_at", "updated_at"),)

    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    last_user_query_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SessionCheckpointRecord(Base):
    """映射每个诊断会话唯一的最新安全 checkpoint。

    ``snapshot`` 保存 ``session-checkpoint:v1`` JSONB，不保存模型原始输出；session 主键保证一个
    会话只有一个最新快照，source_run 唯一外键保证同一 completed run 不会被多个会话复用。
    checkpoint_version 由应用按成功轮次单调递增，并由数据库正数约束提供最终防线。
    """

    __tablename__ = "session_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "checkpoint_version >= 1",
            name="ck_session_checkpoints_version",
        ),
        UniqueConstraint("source_run_id", name="uq_session_checkpoints_source_run"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class AgentRunRecord(Base):
    """映射一次诊断运行的输入路由、队列租约、终态结果和安全失败摘要。

    JSONB result 保存版本化 DiagnosisRunResult；queued/running 的状态 CheckConstraint、租约字段和
    部分唯一索引共同保证数据库队列不会同 session 并发执行。原异常、worker 身份和 Thought 不进入
    公开模型；worker 身份只用于领取所有权校验。
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "history_trigger IN "
            "('not_requested','user_requested','planner_validation','reusable_signature')",
            name="ck_agent_runs_history_trigger",
        ),
        CheckConstraint(
            "intent IN ('single_component_diagnosis','cross_component_diagnosis')",
            name="ck_agent_runs_intent",
        ),
        CheckConstraint(
            "jsonb_typeof(components) = 'array' AND jsonb_array_length(components) >= 1",
            name="ck_agent_runs_components",
        ),
        CheckConstraint(
            "(status = 'queued' AND result IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND started_at IS NULL AND completed_at IS NULL "
            "AND attempt_count = 0 AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(status = 'running' AND result IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND attempt_count >= 1 AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status = 'completed' AND result IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND attempt_count >= 1 AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(status = 'failed' AND result IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND attempt_count >= 1 AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(status = 'cancelled' AND result IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND completed_at IS NOT NULL "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND ((started_at IS NULL AND attempt_count = 0) OR "
            "(started_at IS NOT NULL AND attempt_count >= 1)))",
            name="ck_agent_runs_terminal_payload",
        ),
        Index("ix_agent_runs_session_created", "session_id", "created_at"),
        Index("ix_agent_runs_queue", "status", "created_at"),
        Index(
            "uq_agent_runs_active_session",
            "session_id",
            unique=True,
            postgresql_where=sa.text("status IN ('queued','running')"),
        ),
    )

    run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(50), nullable=False)
    components: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    history_trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RunEventRecord(Base):
    """映射按 run/sequence 排序的公开检索、ReAct、报告、记忆和系统事件。

    payload 只保存确定性安全投影；唯一约束阻止同一 run 出现重复序号。删除 session 会级联删除 run，
    再级联清理事件，避免孤立时间线。
    """

    __tablename__ = "run_events"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('retrieval','react','report','memory','system')",
            name="ck_run_events_phase",
        ),
        CheckConstraint("sequence >= 1", name="ck_run_events_sequence"),
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        Index("ix_run_events_run_sequence", "run_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RunTraceSpanRecord(Base):
    """映射每次 run 的持久化调用链 span：层级、耗时、状态与结构化属性。

    进程内内存指标在 Worker 重启后即消失，因此 span 与 run 终态写在同一事务里落库，事后仍可回放
    "30 秒花在哪"。CheckConstraint 复刻 Pydantic 契约的关键约束（层级枚举、非负耗时、序号从 1 起、
    父不自引用），保证绕过应用层的手工写入不会产生无法渲染的残树；attributes 只允许存放 ASCII 标识符
    级别的值，Prompt、Thought 与凭据在写入前已被遥测层的正则拒绝。
    """

    __tablename__ = "run_trace_spans"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('workflow','node','react_step','tool_call','retrieval',"
            "'model_call','persistence')",
            name="ck_run_trace_spans_kind",
        ),
        CheckConstraint(
            "status IN ('ok','error','cancelled')",
            name="ck_run_trace_spans_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_run_trace_spans_sequence"),
        CheckConstraint("duration_ms >= 0", name="ck_run_trace_spans_duration"),
        CheckConstraint("ended_at >= started_at", name="ck_run_trace_spans_interval"),
        CheckConstraint(
            "parent_span_id IS NULL OR parent_span_id <> span_id",
            name="ck_run_trace_spans_parent",
        ),
        UniqueConstraint("run_id", "sequence", name="uq_run_trace_spans_run_sequence"),
        Index("ix_run_trace_spans_run_sequence", "run_id", "sequence"),
        Index("ix_run_trace_spans_kind_name", "kind", "name"),
    )

    span_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
