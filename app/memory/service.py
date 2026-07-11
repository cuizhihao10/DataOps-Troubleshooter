"""实现 Auditor 通过后的确定性案例候选构建、去重合并、状态决策和搜索。

Service 不调用 Chat 模型，也不把 pending 案例注入 Planner。它组合 ReportRunResult、Embedding
Provider 和仓储事务：精确签名优先，随后同组件 pgvector cosine，最终只搜索 confirmed。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from app.domain.models import (
    CaseMemory,
    Component,
    MemoryStatus,
)
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    MemoryDuplicateMatch,
    MemoryStageResult,
    MemoryStageStatus,
    StoredCaseMemory,
)
from app.orchestration import ReportRunResult, ReportWorkflowOutcome
from app.retrieval.embeddings import EmbeddingProvider

_WHITESPACE = re.compile(r"\s+")


class CaseMemoryRepository(Protocol):
    """声明长期记忆 Service 所需的事务内仓储接口。

    生产 PostgreSQL 实现和单元测试替身都必须支持去重锁、精确/向量匹配、写入、状态决策与搜索；
    协议不暴露 ORM Record 或提交方法，事务仍由 runtime 管理。
    """

    async def lock_dedup_scope(self, scope: str) -> None:
        """在当前事务中锁定组件去重范围，并在事务结束时由数据库自动释放。

        ``scope`` 是排序后的组件集合标识；方法无业务返回值，但必须保证同范围并发 staging 串行。
        获取锁失败或事务连接中断时应抛出仓储异常，Service 不得继续执行查重或写入。
        """

        ...

    async def find_exact(self, signature: str) -> MemoryDuplicateMatch | None:
        """按稳定签名查询并锁定精确重复，命中时返回内部存储快照和重复类型。

        ``signature`` 来自组件与规范化根因的 SHA-256；未命中返回 ``None``，查询错误则抛出异常。
        实现必须先于向量查重执行，避免精确重复仍支付 Embedding Provider 调用成本。
        """

        ...

    async def find_similar(
        self,
        embedding: list[float],
        *,
        provider_id: str,
        components: tuple[Component, ...],
        threshold: float,
    ) -> MemoryDuplicateMatch | None:
        """在同组件、Provider 和维度空间内返回达到阈值的最高 cosine 匹配。

        输入向量必须属于 ``provider_id`` 声明的空间，``components`` 限定候选范围，``threshold``
        定义最低相似度；未命中返回 ``None``。维度不兼容、SQL 或 pgvector 失败时应显式抛错。
        """

        ...

    async def insert(self, stored: StoredCaseMemory, *, source_run_id: str) -> None:
        """在当前事务插入新案例及本次来源 Evidence 关联，但不自行提交事务。

        ``stored`` 同时携带公开案例、签名和内部向量，``source_run_id`` 建立审计与幂等边界。
        唯一键、约束或关联写入失败必须使整个事务回滚，不能留下只有主记录的部分状态。
        """

        ...

    async def update(
        self,
        stored: StoredCaseMemory,
        *,
        source_run_id: str,
        source_evidence_refs: list[str],
    ) -> None:
        """更新已锁定案例，并只关联本次 run 的新候选 Evidence，不复制历史关联。

        ``stored`` 是合并后的完整快照，``source_evidence_refs`` 仅代表当前报告证据；方法不提交。
        主记录更新或任一关联失败时必须抛错并回滚，从而保证 occurrence 与证据来源一致。
        """

        ...

    async def has_source_run(self, memory_id: str, source_run_id: str) -> bool:
        """判断指定来源 run 是否已关联该案例，以控制 occurrence_count 的重放幂等。

        两个 ID 都是精确匹配键；已存在关联返回 ``True``，否则返回 ``False`` 且不写数据。
        查询失败必须传播，不能把数据库故障误判为新 run，否则重试会错误增加出现次数。
        """

        ...

    async def set_status(self, memory_id: str, status: MemoryStatus) -> CaseMemory | None:
        """显式更新案例为 confirmed 或 rejected，并返回不含 embedding 的公开领域模型。

        ``memory_id`` 未命中返回 ``None``；实现不得提供恢复 pending 的隐式路径。数据库约束或
        更新失败应抛出异常并由外层事务回滚，避免 API 已响应成功而状态没有持久化。
        """

        ...

    async def search_confirmed(
        self,
        embedding: list[float],
        *,
        provider_id: str,
        limit: int,
    ) -> list[CaseMemoryMatch]:
        """只返回当前 Provider 空间内 confirmed 案例的有界向量匹配列表。

        ``embedding`` 是查询向量，``provider_id`` 隔离数学空间，``limit`` 控制响应预算；无命中
        返回空列表。实现必须在 SQL 层过滤状态，维度或数据库错误则显式抛出而非泄露 pending。
        """

        ...

    async def count_by_status(self) -> MemoryCounts:
        """聚合当前事务快照中的 pending、confirmed 与 rejected 三种状态计数。

        方法不加载 JSONB 案例正文或向量，返回 ``MemoryCounts``，空表返回全零。遇到未知状态应由
        数据库约束阻止；连接或聚合失败必须传播，调用方才能把健康状态标记为不可用。
        """

        ...


class CaseMemoryService:
    """协调审计资格、候选投影、两阶段去重、幂等合并和 confirmed 检索。

    仓储与 Embedding Provider 通过依赖注入隔离，Service 不拥有连接或模型客户端。`now_factory`
    允许测试固定时间；生产缺省使用 UTC，所有写操作由外部事务原子提交。
    """

    def __init__(
        self,
        repository: CaseMemoryRepository,
        embedding_provider: EmbeddingProvider,
        *,
        dedup_similarity_threshold: float = 0.92,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        """注入仓储、Embedding Provider、相似度阈值和可选 UTC 时钟。

        阈值限制在零到一；构造不执行数据库或 embedding I/O。Provider ID/维度在每次仓储查询中
        显式传递，避免跨数学空间去重。
        """

        if not 0 <= dedup_similarity_threshold <= 1:
            raise ValueError("memory dedup similarity threshold must be between zero and one")
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._dedup_similarity_threshold = dedup_similarity_threshold
        self._now_factory = now_factory or _utc_now

    async def stage_from_report(self, result: ReportRunResult) -> MemoryStageResult:
        """仅从 Auditor accepted 且含根因的最终报告暂存或合并 pending 案例。

        degraded/非 accept 和无根因报告返回结构化 skipped 且不调用仓储写入；合法候选先按组件范围
        获取事务锁，精确签名优先，未命中才生成向量并查相似案例。重复 run 幂等不增加出现次数。
        """

        if (
            result.outcome is not ReportWorkflowOutcome.ACCEPTED
            or result.state.audit_result is None
            or result.state.audit_result.status.value != "accept"
        ):
            return MemoryStageResult(status=MemoryStageStatus.SKIPPED_NOT_ACCEPTED)
        report = result.state.draft_report
        if report is None or not report.root_causes:
            return MemoryStageResult(status=MemoryStageStatus.SKIPPED_NO_ROOT_CAUSE)

        candidate = _candidate_from_result(result, now=self._now_factory())
        signature = memory_signature(candidate.components, candidate.root_cause)
        scope = "|".join(component.value for component in candidate.components)
        await self._repository.lock_dedup_scope(scope)

        # 精确签名无需先生成向量，避免重复记录每次 staging 都支付不必要的 Provider 成本。
        exact = await self._repository.find_exact(signature)
        if exact is not None:
            return await self._merge_duplicate(
                exact,
                candidate,
                source_run_id=result.state.run_id,
            )

        candidate_embedding = await self._embed_memory(candidate)
        similar = await self._repository.find_similar(
            candidate_embedding,
            provider_id=self._embedding_provider.provider_id,
            components=tuple(candidate.components),
            threshold=self._dedup_similarity_threshold,
        )
        if similar is not None:
            return await self._merge_duplicate(
                similar,
                candidate,
                source_run_id=result.state.run_id,
            )

        stored = StoredCaseMemory(
            memory=candidate,
            signature=signature,
            embedding=candidate_embedding,
            embedding_provider=self._embedding_provider.provider_id,
            embedding_dimensions=self._embedding_provider.dimensions,
        )
        await self._repository.insert(stored, source_run_id=result.state.run_id)
        return MemoryStageResult(status=MemoryStageStatus.STAGED, memory=candidate)

    async def decide(
        self,
        memory_id: str,
        decision: MemoryDecision,
    ) -> CaseMemory | None:
        """把用户 confirm/reject 决策映射为终态 MemoryStatus 并返回更新案例。

        confirm/reject 可互相切换以支持取消确认和纠错；同目标状态幂等。未命中返回 None，由 API
        转成 404。Service 不允许模型或调用方把案例恢复为 pending。
        """

        target = (
            MemoryStatus.CONFIRMED if decision is MemoryDecision.CONFIRM else MemoryStatus.REJECTED
        )
        return await self._repository.set_status(memory_id, target)

    async def search_confirmed(self, query: str, *, limit: int = 5) -> list[CaseMemoryMatch]:
        """嵌入非空查询并只召回当前 Provider 空间中的 confirmed 案例。

        limit 限制 1..20；Provider 必须返回恰好一个固定维度向量。历史结果仅供后续 capability
        参考，调用方仍必须让本次实时 Observation 优先。
        """

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("memory search query must not be blank")
        if not 1 <= limit <= 20:
            raise ValueError("memory search limit must be between 1 and 20")
        vectors = await self._embedding_provider.embed_texts([normalized_query])
        if len(vectors) != 1 or len(vectors[0]) != self._embedding_provider.dimensions:
            raise ValueError("embedding provider returned an invalid memory query vector")
        return await self._repository.search_confirmed(
            vectors[0],
            provider_id=self._embedding_provider.provider_id,
            limit=limit,
        )

    async def count_by_status(self) -> MemoryCounts:
        """委托仓储返回 pending/confirmed/rejected 数量，不加载案例内容。

        该方法供健康检查和测试使用，只读且不触发 embedding；数据库错误原样传播。
        """

        return await self._repository.count_by_status()

    async def _merge_duplicate(
        self,
        match: MemoryDuplicateMatch,
        candidate: CaseMemory,
        *,
        source_run_id: str,
    ) -> MemoryStageResult:
        """合并重复案例字段、幂等 occurrence、重新嵌入并更新已有记录。

        canonical memory_id/root_cause/status/signature 保留旧记录；列表按首次出现顺序合并。相同 run
        只补新增信息不加计数；若完全无变化则不写数据库。向量基于合并后文本重算，保持存储内容
        与检索表示一致。
        """

        existing = match.stored.memory
        already_seen = await self._repository.has_source_run(existing.memory_id, source_run_id)
        # 使用 model_validate 重建而非无校验 model_copy，确保第三方仓储返回也重新经过时间/去重规则。
        merged_payload = existing.model_dump()
        merged_payload.update(
            symptoms=_stable_unique([*existing.symptoms, *candidate.symptoms]),
            fault_path=_stable_unique([*existing.fault_path, *candidate.fault_path]),
            solution_steps=_stable_unique([*existing.solution_steps, *candidate.solution_steps]),
            components=_sorted_components([*existing.components, *candidate.components]),
            tags=_stable_unique([*existing.tags, *candidate.tags]),
            evidence_refs=_stable_unique([*existing.evidence_refs, *candidate.evidence_refs]),
            occurrence_count=existing.occurrence_count + (0 if already_seen else 1),
        )
        merged_without_time = CaseMemory.model_validate(merged_payload)
        if merged_without_time == existing:
            return MemoryStageResult(
                status=MemoryStageStatus.MERGED,
                memory=existing,
                duplicate_type=match.duplicate_type,
                similarity=match.similarity,
            )

        next_updated_at = max(self._now_factory(), existing.updated_at)
        merged = CaseMemory.model_validate(
            {**merged_without_time.model_dump(), "updated_at": next_updated_at}
        )
        merged_embedding = await self._embed_memory(merged)
        stored = StoredCaseMemory(
            memory=merged,
            signature=match.stored.signature,
            embedding=merged_embedding,
            embedding_provider=self._embedding_provider.provider_id,
            embedding_dimensions=self._embedding_provider.dimensions,
        )
        await self._repository.update(
            stored,
            source_run_id=source_run_id,
            source_evidence_refs=candidate.evidence_refs,
        )
        return MemoryStageResult(
            status=MemoryStageStatus.MERGED,
            memory=merged,
            duplicate_type=match.duplicate_type,
            similarity=match.similarity,
        )

    async def _embed_memory(self, memory: CaseMemory) -> list[float]:
        """将完整结构化案例转换为一个固定维度 embedding，并校验 Provider 数量契约。

        文本按症状、根因、路径、方案、组件和标签稳定组合；不包含 memory_id、状态或时间，避免
        管理元数据改变语义相似度。Provider 错误阻止整个事务提交。
        """

        vectors = await self._embedding_provider.embed_texts([_memory_text(memory)])
        if len(vectors) != 1 or len(vectors[0]) != self._embedding_provider.dimensions:
            raise ValueError("embedding provider returned an invalid case memory vector")
        return vectors[0]


def memory_signature(components: list[Component], root_cause: str) -> str:
    """根据排序组件和规范化根因生成 64 位 SHA-256 精确去重签名。

    NFKC 等复杂语言归一化由 embedding 负责；签名只做大小写、首尾和连续空白规范，避免过度合并
    不同中文根因。输入组件/根因为空时显式失败。
    """

    ordered_components = sorted({component.value for component in components})
    normalized_root = _normalize_text(root_cause)
    if not ordered_components or not normalized_root:
        raise ValueError("memory signature requires components and root cause")
    payload = "|".join([*ordered_components, normalized_root])
    return sha256(payload.encode("utf-8")).hexdigest()


def _candidate_from_result(result: ReportRunResult, *, now: datetime) -> CaseMemory:
    """从 accepted 报告选择最高置信根因并投影为默认 pending CaseMemory。

    根因必须精确匹配至少一个状态假设；症状/组件来自这些假设，路径和方案来自最终报告，证据使用
    报告级可审计引用。缺失匹配视为工作流契约错误并抛出，而不是用用户 query 猜事实。
    """

    if now.tzinfo is None:
        raise ValueError("memory candidate timestamp must include a timezone")
    report = result.state.draft_report
    if report is None or not report.root_causes:
        raise ValueError("memory candidate requires a report root cause")
    conclusion = sorted(
        report.root_causes,
        key=lambda item: (-item.confidence, item.root_cause),
    )[0]
    hypotheses = [
        hypothesis
        for hypothesis in result.state.hypotheses
        if hypothesis.candidate_root_cause == conclusion.root_cause
    ]
    if not hypotheses:
        raise ValueError("accepted memory root cause must match an AgentState hypothesis")
    components = _sorted_components(
        [component for hypothesis in hypotheses for component in hypothesis.components]
    )
    symptoms = _stable_unique([hypothesis.symptom for hypothesis in hypotheses])
    evidence_refs = _stable_unique([*conclusion.evidence_refs, *report.evidence_refs])
    if not evidence_refs:
        raise ValueError("memory candidate requires evidence references")
    signature = memory_signature(components, conclusion.root_cause)
    memory_id = f"mem_{signature[:16]}"
    tags = [*components, result.state.intent or "unknown_intent"]
    return CaseMemory(
        memory_id=memory_id,
        symptoms=symptoms,
        root_cause=conclusion.root_cause,
        fault_path=[step.description for step in report.fault_chain],
        solution_steps=[step.action for step in report.remediation_steps],
        components=components,
        tags=_stable_unique(
            [item.value if isinstance(item, Component) else str(item) for item in tags]
        ),
        evidence_refs=evidence_refs,
        status=MemoryStatus.PENDING,
        occurrence_count=1,
        created_at=now,
        updated_at=now,
    )


def _memory_text(memory: CaseMemory) -> str:
    """按稳定字段顺序组合案例语义内容，作为去重和搜索 embedding 输入。

    管理字段（ID、status、occurrence、时间）不进入文本；换行区分症状、根因、路径、方案和标签，
    同一结构化内容跨状态切换保持相同向量。
    """

    return "\n".join(
        [
            "症状: " + " | ".join(memory.symptoms),
            "根因: " + memory.root_cause,
            "路径: " + " | ".join(memory.fault_path),
            "方案: " + " | ".join(memory.solution_steps),
            "组件: " + " | ".join(component.value for component in memory.components),
            "标签: " + " | ".join(memory.tags),
        ]
    )


def _normalize_text(value: str) -> str:
    """执行 casefold、strip 和连续空白折叠，供精确根因签名使用。

    函数不删除标点或做同义词映射，避免精确签名过度合并；更宽松语义由 pgvector 第二阶段承担。
    """

    return _WHITESPACE.sub(" ", value.casefold().strip())


def _sorted_components(items: list[Component]) -> list[Component]:
    """按组件字符串排序并去重，稳定数据库 JSONB 比较和签名范围。

    返回新的 Component 列表，不修改假设或旧案例；空输入由 CaseMemory/签名边界随后拒绝。
    """

    return [Component(value) for value in sorted({item.value for item in items})]


def _stable_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重字符串，用于合并症状、路径、方案、标签和证据。

    dict 保留插入顺序；空输入返回空列表，是否允许为空由具体 CaseMemory 字段决定。
    """

    return list(dict.fromkeys(items))


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，作为生产案例创建/更新时间来源。

    独立函数便于测试通过 now_factory 替换，不读取本地时区或使用无时区 datetime。
    """

    return datetime.now(UTC)
