"""实现案例记忆的 PostgreSQL/pgvector 去重、状态更新和 confirmed 向量/图融合检索仓储。

仓储只负责 SQL 与 ORM/领域转换，不决定报告是否可写入。AsyncSession 由调用方管理事务；精确签名
和向量检索均在数据库执行，SIMILAR_TO join 补充图邻居，memory_evidence 保留来源 run 审计关联。
"""

from __future__ import annotations

from math import isfinite

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.models import CaseMemory, Component, MemoryStatus
from app.memory.graph_registration import CASE_GRAPH_SOURCE_ID
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDuplicateMatch,
    MemoryDuplicateType,
    MemoryRetrievalChannel,
    MemoryRetrievalMode,
    StoredCaseMemory,
)
from app.persistence.models import (
    CaseMemoryRecord,
    KnowledgeEdgeRecord,
    KnowledgeNodeRecord,
    MemoryEvidenceRecord,
)
from app.retrieval.models import KnowledgeNodeType, KnowledgeRelationType


class PostgresCaseMemoryRepository:
    """封装案例去重锁、CRUD、证据关联和 confirmed cosine 查询。

    仓储不自动 commit/rollback，Service 可把查重、合并、向量更新和证据关联放入同一事务。所有
    Provider/维度、limit 和阈值在 SQL 前校验，数据库异常不吞掉。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入调用方拥有的短生命周期 AsyncSession。

        构造不打开连接或执行查询；同一 Service 操作共享该会话以获得事务一致性。调用方负责
        `begin`/commit/rollback 和关闭，仓储不能把会话保存到进程级状态。
        """

        self._session = session

    async def lock_dedup_scope(self, scope: str) -> None:
        """获取当前事务级 PostgreSQL advisory lock，串行化同组件候选查重。

        scope 由排序组件组成且不能为空；`hashtext` 只生成锁键，不用于安全签名。锁在事务结束时
        自动释放，即使后续异常回滚也不会泄漏，避免并发精确签名插入或相似候选重复创建。
        """

        if not scope.strip():
            raise ValueError("memory dedup scope must not be blank")
        # 事务级 advisory lock 无需显式 unlock，commit/rollback 都会由 PostgreSQL 自动释放。
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
            {"scope": f"case_memory:{scope}"},
        )

    async def find_exact(self, signature: str) -> MemoryDuplicateMatch | None:
        """按 64 位稳定签名查找并锁定精确重复案例。

        调用方应先获取 dedup scope；`FOR UPDATE` 防止同事务范围外的状态决策与合并互相覆盖。未命中
        返回 None，命中固定 similarity=1 和 exact_signature，不提交事务。
        """

        if len(signature) != 64:
            raise ValueError("memory signature must be a 64-character SHA-256 hex digest")
        record = await self._session.scalar(
            select(CaseMemoryRecord)
            .where(CaseMemoryRecord.signature == signature)
            .with_for_update()
        )
        if record is None:
            return None
        return MemoryDuplicateMatch(
            stored=_stored_from_record(record),
            duplicate_type=MemoryDuplicateType.EXACT_SIGNATURE,
            similarity=1.0,
        )

    async def find_similar(
        self,
        embedding: list[float],
        *,
        provider_id: str,
        components: tuple[Component, ...],
        threshold: float,
    ) -> MemoryDuplicateMatch | None:
        """在相同组件和向量空间中查找达到阈值的最相似案例并锁定记录。

        PostgreSQL `<=>` 计算 cosine distance，Python 只把最高一行转换为零到一相似度。组件 JSONB
        使用排序值精确匹配，避免 LTS 与跨组件案例因文本相似被误合并；未达阈值返回 None。
        """

        _validate_embedding(embedding, provider_id=provider_id)
        if not 0 <= threshold <= 1:
            raise ValueError("memory dedup threshold must be between zero and one")
        component_values = [component.value for component in components]
        if not component_values:
            raise ValueError("memory similarity search requires components")

        # 只比较 Provider、维度和组件完全相同的记录，避免数学空间或故障范围混合。
        distance = CaseMemoryRecord.embedding.cosine_distance(embedding)
        row = (
            await self._session.execute(
                select(CaseMemoryRecord, distance.label("cosine_distance"))
                .where(
                    CaseMemoryRecord.embedding_provider == provider_id,
                    CaseMemoryRecord.embedding_dimensions == len(embedding),
                    CaseMemoryRecord.components == component_values,
                )
                .order_by(distance, CaseMemoryRecord.updated_at.desc(), CaseMemoryRecord.memory_id)
                .limit(1)
                .with_for_update()
            )
        ).first()
        if row is None:
            return None
        record, raw_distance = row
        similarity = _similarity(raw_distance)
        if similarity < threshold:
            return None
        return MemoryDuplicateMatch(
            stored=_stored_from_record(record),
            duplicate_type=MemoryDuplicateType.VECTOR_SIMILARITY,
            similarity=similarity,
        )

    async def insert(self, stored: StoredCaseMemory, *, source_run_id: str) -> None:
        """插入一个新 pending/显式状态案例及本次 run 的证据关联，但不提交事务。

        signature 唯一约束和 memory_id 主键是 advisory lock 之外的最终并发防线；Record 字段显式
        映射，embedding 不进入 CaseMemory。证据关联使用 ON CONFLICT DO NOTHING 保持重放幂等。
        """

        record = _record_from_stored(stored)
        self._session.add(record)
        # flush 让唯一/CheckConstraint 在关联外键插入前失败，错误位置更清晰。
        await self._session.flush()
        await self.add_evidence_links(
            stored.memory.memory_id,
            source_run_id=source_run_id,
            evidence_refs=stored.memory.evidence_refs,
        )

    async def update(
        self,
        stored: StoredCaseMemory,
        *,
        source_run_id: str,
        source_evidence_refs: list[str],
    ) -> None:
        """覆盖已锁定案例的合并字段/向量，并幂等追加本次证据关联。

        Service 负责保持 memory_id、created_at 和既有 status；仓储再次按主键查询并锁定，缺失时
        抛 LookupError 而不是插入新行。`source_evidence_refs` 只包含当前候选证据，避免把旧证据错误
        复制到新的 source_run 审计关联。更新不改变 signature 唯一语义，也不自动 commit。
        """

        record = await self._session.scalar(
            select(CaseMemoryRecord)
            .where(CaseMemoryRecord.memory_id == stored.memory.memory_id)
            .with_for_update()
        )
        if record is None:
            raise LookupError(f"case memory not found: {stored.memory.memory_id}")
        _apply_stored(record, stored)
        await self._session.flush()
        await self.add_evidence_links(
            stored.memory.memory_id,
            source_run_id=source_run_id,
            evidence_refs=source_evidence_refs,
        )

    async def add_evidence_links(
        self,
        memory_id: str,
        *,
        source_run_id: str,
        evidence_refs: list[str],
    ) -> None:
        """为案例批量追加来源 run/evidence 关联，重复复合键静默保持幂等。

        输入 ID/引用均不能为空；数据库外键保证 memory_id 存在。该方法不修改 CaseMemory 的 JSONB
        evidence_refs，Service 必须先合并两处数据并在同一事务写入。
        """

        if not memory_id.strip() or not source_run_id.strip():
            raise ValueError("memory_id and source_run_id must not be blank")
        if not evidence_refs or any(not item.strip() for item in evidence_refs):
            raise ValueError("memory evidence refs must be non-empty strings")
        for evidence_ref in dict.fromkeys(evidence_refs):
            statement = insert(MemoryEvidenceRecord).values(
                memory_id=memory_id,
                evidence_ref=evidence_ref,
                source_run_id=source_run_id,
            )
            statement = statement.on_conflict_do_nothing(
                index_elements=[
                    MemoryEvidenceRecord.memory_id,
                    MemoryEvidenceRecord.evidence_ref,
                    MemoryEvidenceRecord.source_run_id,
                ]
            )
            await self._session.execute(statement)

    async def has_source_run(self, memory_id: str, source_run_id: str) -> bool:
        """检查某个 run 是否已为案例贡献过任意 evidence 关联。

        Service 用该结果决定 occurrence_count 是否增加；同 run 新增证据可更新引用但不能重复计数。
        查询只返回布尔存在性，不加载关联内容或提交事务。
        """

        exists = await self._session.scalar(
            select(func.count())
            .select_from(MemoryEvidenceRecord)
            .where(
                MemoryEvidenceRecord.memory_id == memory_id,
                MemoryEvidenceRecord.source_run_id == source_run_id,
            )
        )
        return bool(exists)

    async def get(self, memory_id: str, *, for_update: bool = False) -> CaseMemory | None:
        """按 memory_id 读取案例，并可为状态决策获取行锁。

        未命中返回 None；for_update 只能在调用方事务中使用，锁随事务释放。转换后不返回 embedding，
        防止 API 或 Planner 意外携带大向量。
        """

        if not memory_id.strip():
            raise ValueError("memory_id must not be blank")
        statement = select(CaseMemoryRecord).where(CaseMemoryRecord.memory_id == memory_id)
        if for_update:
            statement = statement.with_for_update()
        record = await self._session.scalar(statement)
        return _memory_from_record(record) if record is not None else None

    async def get_stored(
        self,
        memory_id: str,
        *,
        for_update: bool = False,
    ) -> StoredCaseMemory | None:
        """按 ID 读取包含 embedding 元数据的内部案例快照，并可获取事务行锁。

        该方法只供 memory/persistence 边界同步 GraphRAG 使用，不能作为 API 或 Planner 响应；未命中
        返回 ``None``。``for_update`` 锁随调用方事务释放，数据库污染会在 ``StoredCaseMemory``
        转换时显式失败，避免损坏向量进入图注册流程。
        """

        if not memory_id.strip():
            raise ValueError("memory_id must not be blank")
        statement = select(CaseMemoryRecord).where(CaseMemoryRecord.memory_id == memory_id)
        if for_update:
            statement = statement.with_for_update()
        record = await self._session.scalar(statement)
        return _stored_from_record(record) if record is not None else None

    async def set_status(self, memory_id: str, status: MemoryStatus) -> CaseMemory | None:
        """把案例显式切换为 confirmed/rejected，并支持同目标幂等与纠错反向切换。

        pending 只能由 staging 创建，不能通过用户决策恢复；confirm/reject 可在两个终态间切换，满足
        取消确认/纠错需求。未命中返回 None，更新时间由数据库刷新，事务由调用方提交。
        """

        if status is MemoryStatus.PENDING:
            raise ValueError("memory decisions cannot set status back to pending")
        record = await self._session.scalar(
            select(CaseMemoryRecord)
            .where(CaseMemoryRecord.memory_id == memory_id)
            .with_for_update()
        )
        if record is None:
            return None
        if record.status != status.value:
            record.status = status.value
            record.updated_at = func.now()
            await self._session.flush()
            # refresh 把数据库 now() 解析为带时区 datetime，供 Pydantic CaseMemory 返回。
            await self._session.refresh(record)
        return _memory_from_record(record)

    async def delete(self, memory_id: str) -> CaseMemory | None:
        """删除案例主记录并返回删除前的领域快照。

        行先加锁再删除，保证与 confirm/reject 决策串行；``memory_evidence`` 通过
        外键 ``ON DELETE CASCADE`` 清理，调用方还需在同一事务删除动态 GraphRAG
        节点。返回快照让 runtime/API 能确认删除对象，但不会在响应中携带 embedding。
        """

        if not memory_id.strip():
            raise ValueError("memory_id must not be blank")
        record = await self._session.scalar(
            select(CaseMemoryRecord)
            .where(CaseMemoryRecord.memory_id == memory_id)
            .with_for_update()
        )
        if record is None:
            return None
        memory = _memory_from_record(record)
        await self._session.delete(record)
        await self._session.flush()
        return memory

    async def search_confirmed(
        self,
        embedding: list[float],
        *,
        provider_id: str,
        limit: int,
        mode: MemoryRetrievalMode = MemoryRetrievalMode.VECTOR_GRAPH,
    ) -> list[CaseMemoryMatch]:
        """合并 confirmed pgvector 直接命中与 ``SIMILAR_TO`` 图邻居并返回最终 top-k。

        第一阶段在 compatible Provider/维度空间取直接 top-k；第二阶段只从这些种子的动态 case
        节点沿本组件拥有的相似边扩展 confirmed 邻居。图传播分为种子直接分乘边权，两路按 memory
        ID 去重后取较强分并稳定排序。pending/rejected 在两条 SQL 路径均被排除。
        """

        _validate_embedding(embedding, provider_id=provider_id)
        if not 1 <= limit <= 20:
            raise ValueError("memory search limit must be between 1 and 20")
        distance = CaseMemoryRecord.embedding.cosine_distance(embedding)
        result = await self._session.execute(
            select(CaseMemoryRecord, distance.label("cosine_distance"))
            .where(
                CaseMemoryRecord.status == MemoryStatus.CONFIRMED.value,
                CaseMemoryRecord.embedding_provider == provider_id,
                CaseMemoryRecord.embedding_dimensions == len(embedding),
            )
            .order_by(distance, CaseMemoryRecord.updated_at.desc(), CaseMemoryRecord.memory_id)
            .limit(limit)
        )
        direct_matches = [
            CaseMemoryMatch(
                memory=_memory_from_record(record),
                similarity=(similarity := _similarity(raw_distance)),
                retrieval_channels=[MemoryRetrievalChannel.VECTOR],
                direct_similarity=similarity,
            )
            for record, raw_distance in result
        ]
        graph_neighbors = []
        if mode is MemoryRetrievalMode.VECTOR_GRAPH:
            graph_neighbors = await self._search_graph_neighbors(
                direct_matches,
                provider_id=provider_id,
                dimensions=len(embedding),
            )
        return merge_case_memory_matches(
            direct_matches,
            graph_neighbors,
            limit=limit,
        )

    async def _search_graph_neighbors(
        self,
        direct_matches: list[CaseMemoryMatch],
        *,
        provider_id: str,
        dimensions: int,
    ) -> list[tuple[CaseMemory, float, str]]:
        """从直接向量种子的动态 case 节点扩展 confirmed 相似邻居。

        返回三元组为邻居领域案例、``seed_similarity * edge.weight`` 图传播分和稳定 edge ID。查询
        同时校验边来源/类型、两端 case 节点、邻居状态及向量空间；缺少已注册节点时仅没有图候选，
        SQL 或枚举污染仍显式失败。零传播分被排除，因为它不能提供排序增益。
        """

        if not direct_matches:
            return []
        seed_scores = {
            match.memory.memory_id: match.direct_similarity
            for match in direct_matches
            if match.direct_similarity is not None
        }
        if not seed_scores:
            return []

        seed_node = aliased(KnowledgeNodeRecord)
        neighbor_node = aliased(KnowledgeNodeRecord)
        neighbor_memory = aliased(CaseMemoryRecord)
        result = await self._session.execute(
            select(
                neighbor_memory,
                KnowledgeEdgeRecord.edge_id,
                KnowledgeEdgeRecord.weight,
                seed_node.source_id.label("seed_memory_id"),
            )
            .join(seed_node, KnowledgeEdgeRecord.from_node_id == seed_node.node_id)
            .join(neighbor_node, KnowledgeEdgeRecord.to_node_id == neighbor_node.node_id)
            .join(neighbor_memory, neighbor_node.source_id == neighbor_memory.memory_id)
            .where(
                KnowledgeEdgeRecord.relation_type == KnowledgeRelationType.SIMILAR_TO.value,
                KnowledgeEdgeRecord.source_id == CASE_GRAPH_SOURCE_ID,
                seed_node.node_type == KnowledgeNodeType.CASE.value,
                seed_node.source_id.in_(list(seed_scores)),
                neighbor_node.node_type == KnowledgeNodeType.CASE.value,
                neighbor_memory.status == MemoryStatus.CONFIRMED.value,
                neighbor_memory.embedding_provider == provider_id,
                neighbor_memory.embedding_dimensions == dimensions,
            )
            .order_by(
                seed_node.source_id,
                KnowledgeEdgeRecord.weight.desc(),
                KnowledgeEdgeRecord.edge_id,
            )
        )

        neighbors: list[tuple[CaseMemory, float, str]] = []
        for record, edge_id, edge_weight, seed_memory_id in result:
            # 边权只描述两个历史案例的接近程度，必须乘当前查询对种子的直接相似度，防止与本次
            # 问题无关但彼此相似的旧案例仅凭图结构获得高排名。
            graph_score = seed_scores[str(seed_memory_id)] * float(edge_weight)
            if graph_score <= 0:
                continue
            neighbors.append(
                (
                    _memory_from_record(record),
                    max(0.0, min(1.0, graph_score)),
                    str(edge_id),
                )
            )
        return neighbors

    async def count_by_status(self) -> MemoryCounts:
        """在当前事务快照中统计三种记忆状态，供健康检查公开规模。

        查询按 status group，不加载 JSONB 或向量；未知状态理论上被 CheckConstraint 阻止，若出现则
        忽略并由迁移/数据审计另行失败。空表返回三个零。
        """

        rows = await self._session.execute(
            select(CaseMemoryRecord.status, func.count()).group_by(CaseMemoryRecord.status)
        )
        counts = {status: int(count) for status, count in rows}
        return MemoryCounts(
            pending=counts.get(MemoryStatus.PENDING.value, 0),
            confirmed=counts.get(MemoryStatus.CONFIRMED.value, 0),
            rejected=counts.get(MemoryStatus.REJECTED.value, 0),
        )


def merge_case_memory_matches(
    direct_matches: list[CaseMemoryMatch],
    graph_neighbors: list[tuple[CaseMemory, float, str]],
    *,
    limit: int,
) -> list[CaseMemoryMatch]:
    """按 memory ID 合并向量直接候选和图邻居，并生成可解释稳定 top-k。

    ``graph_neighbors`` 的元组依次为 confirmed 案例、图传播分和 edge ID。相同案例可由多条种子边
    到达：只保留最高图分，分数并列时保留全部稳定 edge 引用；若它也直接命中，则通道同时包含
    vector/graph，最终 similarity 取两个分量最大值。非法 limit、非 confirmed 或坏 edge ID 会由
    本函数/Pydantic 显式失败。
    """

    if not 1 <= limit <= 20:
        raise ValueError("memory match merge limit must be between 1 and 20")

    # 中间字典只保存生成最终强类型模型所需的最小分量；先放直接命中，保证同 ID 的公开案例快照
    # 与查询 SQL 一致，图查询只为缺失候选补充 memory。
    candidates: dict[str, dict[str, object]] = {}
    for match in direct_matches:
        candidates[match.memory.memory_id] = {
            "memory": match.memory,
            "direct_similarity": match.direct_similarity,
            "graph_score": None,
            "graph_edge_refs": [],
        }

    for memory, graph_score, edge_id in graph_neighbors:
        if memory.status is not MemoryStatus.CONFIRMED:
            raise ValueError("graph memory neighbors must be confirmed")
        if not 0 <= graph_score <= 1:
            raise ValueError("graph memory score must be between zero and one")
        if not edge_id.startswith("edge_case_similar_"):
            raise ValueError("graph memory edge must use a stable case similarity ID")

        candidate = candidates.setdefault(
            memory.memory_id,
            {
                "memory": memory,
                "direct_similarity": None,
                "graph_score": None,
                "graph_edge_refs": [],
            },
        )
        current_graph_score = candidate["graph_score"]
        if current_graph_score is None or graph_score > float(current_graph_score):
            candidate["graph_score"] = graph_score
            candidate["graph_edge_refs"] = [edge_id]
        elif abs(graph_score - float(current_graph_score)) <= 1e-9:
            edge_refs = candidate["graph_edge_refs"]
            if not isinstance(edge_refs, list):  # pragma: no cover - internal invariant.
                raise TypeError("graph edge accumulator must be a list")
            if edge_id not in edge_refs:
                edge_refs.append(edge_id)

    merged: list[CaseMemoryMatch] = []
    for candidate in candidates.values():
        memory = candidate["memory"]
        if not isinstance(memory, CaseMemory):  # pragma: no cover - internal invariant.
            raise TypeError("memory candidate accumulator must contain CaseMemory")
        direct_similarity = candidate["direct_similarity"]
        graph_score = candidate["graph_score"]
        graph_edge_refs = candidate["graph_edge_refs"]
        channels = []
        component_scores: list[float] = []
        if direct_similarity is not None:
            channels.append(MemoryRetrievalChannel.VECTOR)
            component_scores.append(float(direct_similarity))
        if graph_score is not None:
            channels.append(MemoryRetrievalChannel.GRAPH)
            component_scores.append(float(graph_score))
        if not isinstance(graph_edge_refs, list):  # pragma: no cover - internal invariant.
            raise TypeError("graph edge accumulator must be a list")
        merged.append(
            CaseMemoryMatch(
                memory=memory,
                similarity=max(component_scores),
                retrieval_channels=channels,
                direct_similarity=(None if direct_similarity is None else float(direct_similarity)),
                graph_score=None if graph_score is None else float(graph_score),
                graph_edge_refs=sorted(str(reference) for reference in graph_edge_refs),
            )
        )

    # 最终分优先；分数相同时直接命中优先于纯图扩展，再比较图分、新鲜度和稳定 ID，保证分页/测试
    # 不依赖 PostgreSQL 未声明的行顺序。
    return sorted(
        merged,
        key=lambda match: (
            -match.similarity,
            -(match.direct_similarity if match.direct_similarity is not None else -1),
            -(match.graph_score if match.graph_score is not None else -1),
            -match.memory.updated_at.timestamp(),
            match.memory.memory_id,
        ),
    )[:limit]


def _record_from_stored(stored: StoredCaseMemory) -> CaseMemoryRecord:
    """把内部 StoredCaseMemory 显式映射为新的 ORM Record。

    所有列表复制为普通 JSONB 值，组件序列保存枚举字符串；函数不携带 Pydantic 私有状态，也不
    自动 add/flush。数据库约束会再次验证状态、计数和向量维度。
    """

    memory = stored.memory
    return CaseMemoryRecord(
        memory_id=memory.memory_id,
        signature=stored.signature,
        symptoms=list(memory.symptoms),
        root_cause=memory.root_cause,
        fault_path=list(memory.fault_path),
        solution_steps=list(memory.solution_steps),
        components=[component.value for component in memory.components],
        tags=list(memory.tags),
        evidence_refs=list(memory.evidence_refs),
        status=memory.status.value,
        occurrence_count=memory.occurrence_count,
        embedding=list(stored.embedding),
        embedding_provider=stored.embedding_provider,
        embedding_dimensions=stored.embedding_dimensions,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def _apply_stored(record: CaseMemoryRecord, stored: StoredCaseMemory) -> None:
    """把已合并 StoredCaseMemory 覆盖到锁定 ORM Record，保持单行更新语义。

    memory_id 不允许变化；Service 负责保留 created_at/status。显式赋值让代码评审可看到哪些字段会
    被重复案例更新，避免 ORM merge 意外覆盖未列出的数据库元数据。
    """

    memory = stored.memory
    if record.memory_id != memory.memory_id:
        raise ValueError("cannot update a different case memory ID")
    record.signature = stored.signature
    record.symptoms = list(memory.symptoms)
    record.root_cause = memory.root_cause
    record.fault_path = list(memory.fault_path)
    record.solution_steps = list(memory.solution_steps)
    record.components = [component.value for component in memory.components]
    record.tags = list(memory.tags)
    record.evidence_refs = list(memory.evidence_refs)
    record.status = memory.status.value
    record.occurrence_count = memory.occurrence_count
    record.embedding = list(stored.embedding)
    record.embedding_provider = stored.embedding_provider
    record.embedding_dimensions = stored.embedding_dimensions
    record.created_at = memory.created_at
    record.updated_at = memory.updated_at


def _stored_from_record(record: CaseMemoryRecord) -> StoredCaseMemory:
    """把 ORM Record 转换为带内部向量元数据的受校验 StoredCaseMemory。

    pgvector 可能返回数组样对象，先转 list；Pydantic 再验证维度、有限值和领域时间/集合约束。
    """

    return StoredCaseMemory(
        memory=_memory_from_record(record),
        signature=record.signature,
        embedding=list(record.embedding),
        embedding_provider=record.embedding_provider,
        embedding_dimensions=record.embedding_dimensions,
    )


def _memory_from_record(record: CaseMemoryRecord) -> CaseMemory:
    """把数据库案例字段投影为不含 embedding 的 CaseMemory 领域对象。

    显式 Component/MemoryStatus 枚举转换可在数据库约束漂移时失败；返回对象可安全进入 API 和
    Planner confirmed 上下文，不包含 ORM 会话或向量。
    """

    return CaseMemory(
        memory_id=record.memory_id,
        symptoms=list(record.symptoms),
        root_cause=record.root_cause,
        fault_path=list(record.fault_path),
        solution_steps=list(record.solution_steps),
        components=[Component(item) for item in record.components],
        tags=list(record.tags),
        evidence_refs=list(record.evidence_refs),
        status=MemoryStatus(record.status),
        occurrence_count=record.occurrence_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _validate_embedding(embedding: list[float], *, provider_id: str) -> None:
    """在任何 pgvector SQL 前校验查询向量和 Provider ID。

    非空、8..4096、有限、非零约束与 StoredCaseMemory 一致；失败不会向数据库发送不可比较向量，
    也不会让空 Provider 绕过数学空间过滤。
    """

    if not provider_id.strip():
        raise ValueError("embedding provider_id must not be blank")
    if not 8 <= len(embedding) <= 4096:
        raise ValueError("memory embedding dimensions must be between 8 and 4096")
    if not all(isinstance(value, int | float) and isfinite(value) for value in embedding):
        raise ValueError("memory embedding values must be finite numbers")
    if not any(value != 0 for value in embedding):
        raise ValueError("memory embedding must not be all zero")


def _similarity(raw_distance) -> float:
    """把 pgvector cosine distance 转成裁剪到零到一的公开相似度。

    cosine 理论相似度可为负，历史匹配契约只公开零到一相关度，因此负相关裁剪为零；浮点微小越界
    同样被集中处理，不影响数据库排序。
    """

    return max(0.0, min(1.0, 1.0 - float(raw_distance)))
