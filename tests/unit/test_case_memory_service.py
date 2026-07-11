"""验证审计后案例候选、精确/向量去重、run 幂等和 confirmed 搜索服务。

测试使用内存仓储替身与真实确定性 Embedding Provider，不访问 PostgreSQL；目标是精确验证 Service
资格门禁、字段投影、合并计数、状态保留和调用顺序，SQL 行为由 postgres 集成测试覆盖。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    AuditResult,
    AuditStatus,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
)
from app.memory.models import (
    CaseMemoryMatch,
    MemoryCounts,
    MemoryDecision,
    MemoryDuplicateMatch,
    MemoryDuplicateType,
    MemoryStageStatus,
    StoredCaseMemory,
)
from app.memory.service import CaseMemoryService
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    ReportEventType,
    ReportPublicEvent,
    ReportRunResult,
    ReportWorkflowOutcome,
)
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


class FixedClock:
    """返回同一个带时区时间，供候选创建和幂等测试稳定重放。

    类比 lambda 更适合学习型项目，因为 `__call__` 可以解释输入、输出和边界；实例不读取系统时钟。
    """

    def __init__(self, value: datetime) -> None:
        """保存一个带时区 datetime，拒绝无法跨环境解释的 naive 时间。

        构造不复制或转换时区，调用时原样返回，便于精确断言数据库字段。输入没有 ``tzinfo`` 时
        立即抛出 ``ValueError``，避免测试意外依赖运行机器的本地时区解释。
        """

        if value.tzinfo is None:
            raise ValueError("fixed clock requires timezone-aware datetime")
        self._value = value

    def __call__(self) -> datetime:
        """返回构造时保存的 datetime，不产生副作用或推进时间。

        相同实例每次调用结果一致，适合验证同 run 重放不会因时钟变化触发更新。方法没有输入，
        也不会抛出运行时异常；若构造值非法，错误已在初始化阶段提前暴露。
        """

        return self._value


class SequenceClock:
    """按预设顺序返回多个带时区时间，超额调用时显式失败。

    该测试替身用于验证新增/合并的时间调用次数，不使用真实 sleep 或系统时钟。返回值完全由
    输入序列决定，因此失败时可以区分“时间预算多调用”与业务字段断言失败。
    """

    def __init__(self, values: list[datetime]) -> None:
        """复制时间序列并拒绝空列表或 naive datetime。

        复制避免外部列表被就地消费；每个测试实例独占序列，调用超额抛 ``AssertionError``。
        空输入或任一 naive datetime 立即抛 ``ValueError``，不允许模糊的本地时区进入时间断言。
        """

        if not values or any(value.tzinfo is None for value in values):
            raise ValueError("sequence clock requires timezone-aware values")
        self._values = list(values)

    def __call__(self) -> datetime:
        """弹出并返回下一个时间，序列耗尽表示 Service 调用次数超出预期。

        方法不自动重复最后值，以免隐藏无意义的额外 embedding/更新路径。每次调用消费一个输入，
        没有剩余值时抛 ``AssertionError``，把意外的额外时钟读取定位到当前测试。
        """

        if not self._values:
            raise AssertionError("sequence clock was called more times than expected")
        return self._values.pop(0)


class InMemoryMemoryRepository:
    """模拟仓储事务内的签名查重、run 关联、状态决策和 confirmed 搜索。

    替身保存 StoredCaseMemory 而不是松散字典，所有 Service 输出仍经过生产 Pydantic 模型；
    `force_vector_match` 可隔离第二阶段去重，不模拟 SQL 距离算法。
    """

    def __init__(self) -> None:
        """初始化空记录、来源 run 集合、锁记录和可选强制向量匹配开关。

        每个测试创建新实例，避免状态泄漏；构造不生成向量或默认案例。所有集合从空状态开始，
        后续缺失 ID 返回值由各仓储方法定义，避免构造阶段悄悄准备命中数据。
        """

        self.records: dict[str, StoredCaseMemory] = {}
        self.source_runs: dict[str, set[str]] = {}
        self.lock_scopes: list[str] = []
        self.force_vector_match = False
        self.insert_calls = 0
        self.update_calls = 0

    async def lock_dedup_scope(self, scope: str) -> None:
        """记录组件锁范围，模拟事务 advisory lock 已成功获取。

        空 scope 立即失败，正常路径不阻塞；列表用于断言 Service 在任何查重前先锁定范围。
        """

        if not scope:
            raise ValueError("scope must not be empty")
        self.lock_scopes.append(scope)

    async def find_exact(self, signature: str) -> MemoryDuplicateMatch | None:
        """按 StoredCaseMemory.signature 查找精确重复并返回 similarity=1。

        未命中返回 None；查找不修改记录或来源 run。
        """

        stored = next(
            (item for item in self.records.values() if item.signature == signature),
            None,
        )
        if stored is None:
            return None
        return MemoryDuplicateMatch(
            stored=stored,
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
        """在测试开关启用时返回首条同组件记录作为 vector duplicate。

        参数会做最小断言以证明 Service 传入非空向量、Provider 和合法阈值；不开关或无记录返回 None。
        """

        assert embedding
        assert provider_id
        assert components
        assert 0 <= threshold <= 1
        if not self.force_vector_match or not self.records:
            return None
        stored = next(iter(self.records.values()))
        return MemoryDuplicateMatch(
            stored=stored,
            duplicate_type=MemoryDuplicateType.VECTOR_SIMILARITY,
            similarity=0.96,
        )

    async def insert(self, stored: StoredCaseMemory, *, source_run_id: str) -> None:
        """保存新记录并把来源 run 记入集合，重复 memory_id 视为测试错误。

        方法不模拟 commit；Service 调用完成即代表当前内存事务可见。若 ``memory_id`` 已存在则抛出
        ``AssertionError``，用来暴露 Service 在查重后仍错误走新增路径的缺陷。
        """

        if stored.memory.memory_id in self.records:
            raise AssertionError("duplicate insert")
        self.records[stored.memory.memory_id] = stored
        self.source_runs[stored.memory.memory_id] = {source_run_id}
        self.insert_calls += 1

    async def update(
        self,
        stored: StoredCaseMemory,
        *,
        source_run_id: str,
        source_evidence_refs: list[str],
    ) -> None:
        """覆盖现有记录并幂等加入来源 run，缺失 ID 时显式失败。

        update_calls 用于验证同 run 完全重放不会触发无意义数据库更新；source_evidence_refs 非空证明
        Service 没有把旧合并引用重复归属到新 run。
        """

        if stored.memory.memory_id not in self.records:
            raise AssertionError("update requires existing memory")
        assert source_evidence_refs
        self.records[stored.memory.memory_id] = stored
        self.source_runs.setdefault(stored.memory.memory_id, set()).add(source_run_id)
        self.update_calls += 1

    async def has_source_run(self, memory_id: str, source_run_id: str) -> bool:
        """返回来源 run 是否已经关联到案例，供 occurrence 幂等判断。

        未知 ``memory_id`` 视为空集合，不修改状态。该行为对应 SQL ``EXISTS`` 的 false 语义，
        让测试可以验证首次来源会增加 occurrence，而同 run 重放保持不变。
        """

        return source_run_id in self.source_runs.get(memory_id, set())

    async def set_status(self, memory_id: str, status: MemoryStatus):
        """更新案例状态并保留向量/签名，未命中返回 None。

        updated_at 在单元替身中保持不变，测试只关注召回可见性与状态切换；生产仓储由数据库 now
        刷新时间。
        """

        stored = self.records.get(memory_id)
        if stored is None:
            return None
        memory = stored.memory.model_copy(update={"status": status})
        self.records[memory_id] = stored.model_copy(update={"memory": memory})
        return memory

    async def search_confirmed(
        self,
        embedding: list[float],
        *,
        provider_id: str,
        limit: int,
    ) -> list[CaseMemoryMatch]:
        """返回最多 limit 条 confirmed 内存记录并赋固定相似度。

        pending/rejected 在替身中先过滤，模拟 SQL WHERE；参数断言证明 Service 已生成正确向量空间。
        """

        assert embedding
        assert provider_id
        return [
            CaseMemoryMatch(memory=item.memory, similarity=0.9)
            for item in self.records.values()
            if item.memory.status is MemoryStatus.CONFIRMED
        ][:limit]

    async def count_by_status(self) -> MemoryCounts:
        """从内存记录聚合三种状态数量，模拟健康检查查询。

        结果使用生产 ``MemoryCounts``，未知状态无法进入 ``StoredCaseMemory``。空仓储返回全零，
        聚合过程不修改记录，因而能稳定验证健康检查在 confirm/reject 后的快照刷新。
        """

        statuses = [item.memory.status for item in self.records.values()]
        return MemoryCounts(
            pending=statuses.count(MemoryStatus.PENDING),
            confirmed=statuses.count(MemoryStatus.CONFIRMED),
            rejected=statuses.count(MemoryStatus.REJECTED),
        )


def _accepted_result(
    *,
    run_id: str = "run_memory_unit_001",
    evidence_id: str = "ev_memory_unit_001",
    root_cause: str = "上游数据未按时就绪",
) -> ReportRunResult:
    """构造 Auditor accepted、含支持假设和可审计根因的报告结果。

    可覆盖 run/evidence/root_cause 以测试重放、第二次 occurrence 和向量相似合并；事件满足报告工作流
    终态契约，不包含 Thought。
    """

    evidence = Evidence(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.TOOL,
        source_id=f"source_{run_id}",
        content="合成工具确认上游数据未就绪。",
        observed_at=NOW,
        reliability=0.95,
    )
    report = DiagnosisReport(
        summary="已确认合成根因。",
        root_causes=[
            RootCauseConclusion(
                root_cause=root_cause,
                confidence=0.9,
                evidence_refs=[evidence_id],
            )
        ],
        evidence_refs=[evidence_id],
        remediation_steps=[
            RemediationStep(
                order=1,
                action="在隔离环境补齐上游数据后人工复核。",
                risk_level=RiskLevel.MEDIUM,
                evidence_refs=[evidence_id],
                prerequisites=["确认数据快照与审批。"],
                rollback="恢复补数前快照。",
                verification="重新执行只读状态检查。",
            )
        ],
        risks=["方案需人工审批。"],
    )
    state = AgentState(
        run_id=run_id,
        session_id=f"session_{run_id}",
        user_query="检查合成任务",
        intent="single_component_diagnosis",
        active_capabilities=[
            "single_component_diagnosis",
            "risk_assessment",
            "structured_reporting",
        ],
        hypotheses=[
            FaultHypothesis(
                hypothesis_id=f"hyp_{run_id}",
                symptom="LTS 任务等待上游",
                candidate_root_cause=root_cause,
                components=[Component.LTS],
                supporting_evidence=[evidence_id],
                status=HypothesisStatus.CONFIRMED,
                confidence=0.9,
            )
        ],
        evidence=[evidence],
        stop_reason="evidence_sufficient",
        draft_report=report,
        audit_result=AuditResult(status=AuditStatus.ACCEPT),
    )
    return ReportRunResult(
        contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
        state=state,
        outcome=ReportWorkflowOutcome.ACCEPTED,
        events=[
            ReportPublicEvent(
                event_id="report_evt_0123456789abcdef",
                sequence=1,
                event_type=ReportEventType.DRAFT_CREATED,
                summary="合成草稿已创建。",
                revision_number=0,
            ),
            ReportPublicEvent(
                event_id="report_evt_fedcba9876543210",
                sequence=2,
                event_type=ReportEventType.AUDIT_COMPLETED,
                summary="合成审计已接受。",
                audit_status=AuditStatus.ACCEPT,
                revision_number=0,
            ),
        ],
    )


def _degraded_result() -> ReportRunResult:
    """构造 Auditor revise 且 outcome=degraded 的安全报告结果。

    结果仍带一个结构化根因用于证明 eligibility 门禁优先于内容投影，Service 必须直接 skip 且不锁库。
    """

    accepted = _accepted_result()
    issue = AuditIssue(
        code=AuditIssueCode.UNSUPPORTED_CLAIM,
        claim_path="root_causes[0]",
        message="合成审计未通过。",
    )
    state = accepted.state.model_copy(
        update={
            "audit_result": AuditResult(
                status=AuditStatus.REVISE,
                issues=[issue],
                revision_instructions=["删除结论。"],
            )
        }
    )
    return ReportRunResult(
        contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
        state=state,
        outcome=ReportWorkflowOutcome.DEGRADED,
        events=[
            accepted.events[0],
            ReportPublicEvent(
                event_id="report_evt_0011223344556677",
                sequence=2,
                event_type=ReportEventType.SAFE_DEGRADED,
                summary="合成报告已降级。",
                audit_status=AuditStatus.REVISE,
                issue_codes=(AuditIssueCode.UNSUPPORTED_CLAIM,),
                revision_number=0,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_degraded_report_is_skipped_without_repository_or_embedding_work() -> None:
    """验证未通过 Auditor 的结果绝不获取去重锁或创建记录。

    skipped_not_accepted 明确解释原因；空 lock_scopes/records 证明 Service 在任何数据库/Provider
    工作前执行资格门禁。
    """

    repository = InMemoryMemoryRepository()
    service = CaseMemoryService(repository, DeterministicHashEmbeddingProvider(dimensions=32))

    result = await service.stage_from_report(_degraded_result())

    assert result.status is MemoryStageStatus.SKIPPED_NOT_ACCEPTED
    assert repository.lock_scopes == []
    assert repository.records == {}


@pytest.mark.asyncio
async def test_new_accepted_case_is_staged_pending_with_deterministic_fields() -> None:
    """验证 accepted 报告投影为 pending 案例，并写入签名、向量和来源 run。

    memory_id 对相同组件/根因稳定，症状来自假设而非用户自由文本，方案和证据来自最终报告；默认
    pending 保证尚未进入搜索。
    """

    repository = InMemoryMemoryRepository()
    service = CaseMemoryService(
        repository,
        DeterministicHashEmbeddingProvider(dimensions=32),
        now_factory=FixedClock(NOW),
    )

    result = await service.stage_from_report(_accepted_result())

    assert result.status is MemoryStageStatus.STAGED
    assert result.memory is not None
    assert result.memory.status is MemoryStatus.PENDING
    assert result.memory.memory_id.startswith("mem_")
    assert result.memory.symptoms == ["LTS 任务等待上游"]
    assert result.memory.components == [Component.LTS]
    assert result.memory.evidence_refs == ["ev_memory_unit_001"]
    assert repository.lock_scopes == ["lts"]
    stored = repository.records[result.memory.memory_id]
    assert stored.embedding_dimensions == 32
    assert repository.source_runs[result.memory.memory_id] == {"run_memory_unit_001"}


@pytest.mark.asyncio
async def test_exact_duplicate_is_idempotent_per_run_and_increments_new_occurrence_once() -> None:
    """验证相同 run 重放不更新/计数，新 run 同签名只增加一次 occurrence 并合并证据。

    三次 staging 顺序为新增、同 run 重放、第二 run；最终 occurrence=2、两条证据、一个 memory_id，
    update_calls 仅一次，证明幂等关联生效。
    """

    repository = InMemoryMemoryRepository()
    service = CaseMemoryService(
        repository,
        DeterministicHashEmbeddingProvider(dimensions=32),
        now_factory=SequenceClock(
            [NOW, NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=1)]
        ),
    )

    first = await service.stage_from_report(_accepted_result())
    replay = await service.stage_from_report(_accepted_result())
    second = await service.stage_from_report(
        _accepted_result(
            run_id="run_memory_unit_002",
            evidence_id="ev_memory_unit_002",
        )
    )

    assert first.status is MemoryStageStatus.STAGED
    assert replay.status is MemoryStageStatus.MERGED
    assert replay.duplicate_type is MemoryDuplicateType.EXACT_SIGNATURE
    assert second.status is MemoryStageStatus.MERGED
    assert second.memory is not None
    assert second.memory.occurrence_count == 2
    assert second.memory.evidence_refs == ["ev_memory_unit_001", "ev_memory_unit_002"]
    assert repository.insert_calls == 1
    assert repository.update_calls == 1


@pytest.mark.asyncio
async def test_vector_duplicate_preserves_canonical_root_and_existing_status() -> None:
    """验证不同签名但同组件高相似案例合并到旧 ID，保留 canonical 根因和 confirmed 状态。

    先暂存并确认旧案例，再强制向量匹配一个措辞不同的新根因；结果 duplicate_type=vector，状态仍
    confirmed、root_cause 不被新模型措辞覆盖，occurrence 增加。
    """

    repository = InMemoryMemoryRepository()
    service = CaseMemoryService(
        repository,
        DeterministicHashEmbeddingProvider(dimensions=32),
        dedup_similarity_threshold=0.9,
        now_factory=FixedClock(NOW),
    )
    first = await service.stage_from_report(_accepted_result())
    assert first.memory is not None
    await service.decide(first.memory.memory_id, MemoryDecision.CONFIRM)
    repository.force_vector_match = True

    result = await service.stage_from_report(
        _accepted_result(
            run_id="run_memory_unit_003",
            evidence_id="ev_memory_unit_003",
            root_cause="上游数据到达延迟",
        )
    )

    assert result.status is MemoryStageStatus.MERGED
    assert result.duplicate_type is MemoryDuplicateType.VECTOR_SIMILARITY
    assert result.memory is not None
    assert result.memory.memory_id == first.memory.memory_id
    assert result.memory.root_cause == "上游数据未按时就绪"
    assert result.memory.status is MemoryStatus.CONFIRMED
    assert result.memory.occurrence_count == 2


@pytest.mark.asyncio
async def test_search_visibility_tracks_confirm_reject_and_reconfirm_decisions() -> None:
    """验证 pending/rejected 不可搜索，confirmed 可见，并支持显式重新确认纠错。

    同一案例依次 pending→confirmed→rejected→confirmed；每一步搜索结果与状态一致，证明 Service
    不缓存旧 confirmed 列表，也不会让拒绝记录继续进入默认召回。
    """

    repository = InMemoryMemoryRepository()
    service = CaseMemoryService(
        repository,
        DeterministicHashEmbeddingProvider(dimensions=32),
        now_factory=FixedClock(NOW),
    )
    staged = await service.stage_from_report(_accepted_result())
    assert staged.memory is not None
    memory_id = staged.memory.memory_id

    assert await service.search_confirmed("上游未就绪") == []
    confirmed = await service.decide(memory_id, MemoryDecision.CONFIRM)
    assert confirmed is not None and confirmed.status is MemoryStatus.CONFIRMED
    assert len(await service.search_confirmed("上游未就绪")) == 1
    rejected = await service.decide(memory_id, MemoryDecision.REJECT)
    assert rejected is not None and rejected.status is MemoryStatus.REJECTED
    assert await service.search_confirmed("上游未就绪") == []
    reconfirmed = await service.decide(memory_id, MemoryDecision.CONFIRM)
    assert reconfirmed is not None and reconfirmed.status is MemoryStatus.CONFIRMED
    assert len(await service.search_confirmed("上游未就绪")) == 1
