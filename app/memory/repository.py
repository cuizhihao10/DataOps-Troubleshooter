"""实现案例记忆的 PostgreSQL/pgvector 去重、状态更新和 confirmed 检索仓储。

仓储只负责 SQL 与 ORM/领域转换，不决定报告是否可写入。AsyncSession 由调用方管理事务；精确签名
和向量检索均在数据库执行，memory_evidence 保留每个来源 run 的审计关联。
"""

from __future__ import annotations

from math import isfinite

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import CaseMemory, Component, MemoryStatus
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDuplicateMatch,
    MemoryDuplicateType,
    StoredCaseMemory,
)
from app.persistence.models import CaseMemoryRecord, MemoryEvidenceRecord


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

    async def search_confirmed(
        self,
        embedding: list[float],
        *,
        provider_id: str,
        limit: int,
    ) -> list[CaseMemoryMatch]:
        """在 compatible 向量空间中仅召回 confirmed 案例并按 cosine 相似度排序。

        pending/rejected 在 SQL WHERE 层排除，不依赖 Service 后过滤；limit 为 1..20。返回模型再次
        校验 status=confirmed，形成数据库与领域双重防线。
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
        return [
            CaseMemoryMatch(
                memory=_memory_from_record(record),
                similarity=_similarity(raw_distance),
            )
            for record, raw_distance in result
        ]

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
