"""验证顶层诊断图按需召回 confirmed 案例、复用上下文并在审计后暂存记忆。

测试使用记录型 ReAct/报告/记忆替身，不调用模型、MCP 或数据库；它关注四阶段调用顺序、查询预算、
history trigger、run 身份和 accepted/degraded 的 staging 语义。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
    get_capability_registry,
)
from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    AuditResult,
    AuditStatus,
    CaseMemory,
    Component,
    DiagnosisReport,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
    RootCauseConclusion,
)
from app.memory.models import (
    CaseMemoryMatch,
    MemoryStageResult,
    MemoryStageStatus,
)
from app.orchestration import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    REACT_LOOP_CONTRACT_ID,
    AuditedDiagnosisWorkflow,
    DiagnosisRunRequest,
    DiagnosisWorkflowConfig,
    ReactEventType,
    ReactPublicEvent,
    ReactRunRequest,
    ReactRunResult,
    ReportEventType,
    ReportPublicEvent,
    ReportRunRequest,
    ReportRunResult,
    ReportWorkflowOutcome,
)

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


class RecordingReactWorkflow:
    """记录顶层传入的 ReactRunRequest，并返回同 run 的合成停止结果。

    替身使用真实 capability registry 生成能力快照，因此顶层报告请求仍经过生产 Pydantic 一致性
    校验；它不执行 Planner 或 MCP，调用次数可证明记忆搜索失败时后续阶段没有启动。
    """

    def __init__(self) -> None:
        """初始化空调用列表，不预置状态或历史案例。

        每个测试独占实例，避免不同 history trigger 的请求互相污染；构造不调用 registry、模型或
        工具，所有结果都根据实际传入 request 延迟创建。
        """

        self.requests: list[ReactRunRequest] = []

    async def run(self, request: ReactRunRequest) -> ReactRunResult:
        """保存请求并返回已选择 capability、带停止原因和两条公开事件的结果。

        输入 confirmed 案例会由 ``ReactRunRequest`` 再次校验；输出沿用输入 run/session/Evidence，
        只更新路由字段和 stop_reason。构造错误直接抛出，不能隐藏顶层跨阶段契约缺陷。
        """

        self.requests.append(request)
        selection = get_capability_registry().select(request.capability_request)
        state = request.state.model_copy(
            update={
                "intent": selection.intent.value,
                "active_capabilities": [item.value for item in selection.active_capabilities],
                "stop_reason": "evidence_sufficient",
            }
        )
        return ReactRunResult(
            contract_id=REACT_LOOP_CONTRACT_ID,
            state=state,
            capabilities=selection,
            events=[
                ReactPublicEvent(
                    event_id="react_evt_1111111111111111",
                    sequence=1,
                    event_type=ReactEventType.CAPABILITIES_SELECTED,
                    summary="测试能力选择完成。",
                ),
                ReactPublicEvent(
                    event_id="react_evt_2222222222222222",
                    sequence=2,
                    event_type=ReactEventType.LOOP_STOPPED,
                    summary="测试 ReAct 已停止。",
                    stop_reason="evidence_sufficient",
                ),
            ],
        )


class RecordingReportWorkflow:
    """记录 ReportRunRequest，并按配置返回 accepted 或 degraded 合成报告终态。

    accepted 路径生成有引用根因，degraded 路径生成无根因且含不确定性的安全报告；替身不执行真实
    Auditor，但返回模型完全符合生产 ReportRunResult 终态不变量。
    """

    def __init__(self, outcome: ReportWorkflowOutcome) -> None:
        """保存预期 outcome 并初始化空调用记录。

        ``outcome`` 只能使用生产枚举；每次 run 根据实际 ReAct 状态创建报告，保证 run/session 不会
        被测试常量意外写死。构造不生成报告或调用记忆服务。
        """

        self.outcome = outcome
        self.requests: list[ReportRunRequest] = []

    async def run(self, request: ReportRunRequest) -> ReportRunResult:
        """记录请求并返回与配置 outcome 一致的报告、审计结果和公开事件。

        accepted 使用输入状态中的实时证据引用；degraded 使用结构化 ``report_incomplete`` 问题。
        若输入没有 Evidence，索引访问会显式失败，从而暴露不完整测试/调用契约。
        """

        self.requests.append(request)
        evidence_id = request.state.evidence[0].evidence_id
        if self.outcome is ReportWorkflowOutcome.ACCEPTED:
            report = DiagnosisReport(
                summary="合成诊断已经通过审计。",
                root_causes=[
                    RootCauseConclusion(
                        root_cause="上游数据未按时就绪",
                        confidence=0.9,
                        evidence_refs=[evidence_id],
                    )
                ],
                evidence_refs=[evidence_id],
            )
            audit = AuditResult(status=AuditStatus.ACCEPT)
            final_event = ReportPublicEvent(
                event_id="report_evt_2222222222222222",
                sequence=2,
                event_type=ReportEventType.AUDIT_COMPLETED,
                summary="测试审计接受。",
                audit_status=AuditStatus.ACCEPT,
                revision_number=0,
            )
        else:
            report = DiagnosisReport(
                summary="证据不足，返回安全降级报告。",
                evidence_refs=[evidence_id],
                uncertainties=["尚无足够依据确认根因。"],
            )
            issue = AuditIssue(
                code=AuditIssueCode.REPORT_INCOMPLETE,
                claim_path="root_causes",
                message="缺少可审计根因。",
            )
            audit = AuditResult(
                status=AuditStatus.REVISE,
                issues=[issue],
                revision_instructions=["仅保留不确定性和只读检查。"],
            )
            final_event = ReportPublicEvent(
                event_id="report_evt_3333333333333333",
                sequence=2,
                event_type=ReportEventType.SAFE_DEGRADED,
                summary="测试报告已安全降级。",
                audit_status=AuditStatus.REVISE,
                issue_codes=(AuditIssueCode.REPORT_INCOMPLETE,),
                revision_number=0,
            )
        state = request.state.model_copy(update={"draft_report": report, "audit_result": audit})
        return ReportRunResult(
            contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
            state=state,
            outcome=self.outcome,
            events=[
                ReportPublicEvent(
                    event_id="report_evt_1111111111111111",
                    sequence=1,
                    event_type=ReportEventType.DRAFT_CREATED,
                    summary="测试草稿已创建。",
                    revision_number=0,
                ),
                final_event,
            ],
        )


class RecordingMemoryWorkflow:
    """记录历史搜索与 staging 调用，并返回预设 confirmed 命中和暂存结果。

    替身不计算 embedding 或执行事务；``search_error`` 可证明数据库失败不会被顶层图吞成空历史。
    搜索结果仍使用生产 CaseMemoryMatch，从模型层拒绝未确认案例。
    """

    def __init__(
        self,
        *,
        matches: list[CaseMemoryMatch],
        stage_result: MemoryStageResult,
        search_error: Exception | None = None,
    ) -> None:
        """保存预设结果/异常并初始化查询与报告调用记录。

        列表复制避免测试外部后续修改返回值；staging 结果由生产模型验证。构造不提前抛
        ``search_error``，只有顶层真正执行历史召回时才失败，便于验证 not_requested 跳过语义。
        """

        self.matches = list(matches)
        self.stage_result = stage_result
        self.search_error = search_error
        self.search_calls: list[tuple[str, int | None]] = []
        self.stage_calls: list[ReportRunResult] = []

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[CaseMemoryMatch]:
        """记录 query/limit，按配置抛错或返回不超过 limit 的 confirmed 命中。

        方法不修改案例状态；异常原样抛出，模拟 Provider/SQL 故障。``limit=None`` 返回全部预设值，
        非空预算则稳定切片，便于断言顶层集中配置确实传递到记忆边界。
        """

        self.search_calls.append((query, limit))
        if self.search_error is not None:
            raise self.search_error
        return self.matches if limit is None else self.matches[:limit]

    async def stage(self, result: ReportRunResult) -> MemoryStageResult:
        """记录最终报告并返回预设写入或安全跳过结果。

        替身不重新判断 accepted/degraded；测试通过配置与顶层结果校验共同验证正确组合。若顶层在
        Auditor 前错误调用，本列表中的 ReportRunResult 构造将无法发生并使测试失败。
        """

        self.stage_calls.append(result)
        return self.stage_result


def _initial_state(*, run_id: str = "run_diagnosis_unit_001") -> AgentState:
    """构造含实时 Evidence、历史 Evidence 和当前假设的合成初始状态。

    实时内容应进入 memory query，CASE_MEMORY 内容应被排除以防递归强化；状态尚无 stop/report，
    由记录型 ReAct/报告工作流依次补齐。所有数据均为脱敏合成值。
    """

    return AgentState(
        run_id=run_id,
        session_id=f"session_{run_id}",
        user_query="请检查 LTS 上游未就绪并参考历史案例",
        hypotheses=[
            FaultHypothesis(
                hypothesis_id=f"hyp_{run_id}",
                symptom="LTS 任务等待上游",
                candidate_root_cause="上游数据未按时就绪",
                components=[Component.LTS],
                supporting_evidence=["ev_realtime_001"],
                status=HypothesisStatus.SUPPORTED,
                confidence=0.8,
            )
        ],
        evidence=[
            Evidence(
                evidence_id="ev_realtime_001",
                source_type=EvidenceSourceType.TOOL,
                source_id="lts.get_task_status",
                content="实时工具显示任务仍等待上游数据。",
                observed_at=NOW,
                reliability=0.95,
            ),
            Evidence(
                evidence_id="ev_history_old",
                source_type=EvidenceSourceType.CASE_MEMORY,
                source_id="mem_old",
                content="这段旧案例文本不能再次进入记忆查询。",
                observed_at=NOW,
                reliability=0.8,
            ),
        ],
    )


def _capability_request(history_trigger: HistoryTrigger) -> CapabilitySelectionRequest:
    """创建单组件诊断路由请求并显式传入历史触发来源。

    组件/意图组合满足 registry 约束；函数不解析用户文本，确保测试只改变 history trigger 就能
    验证搜索是否发生，而不会混入路由推断差异。
    """

    return CapabilitySelectionRequest(
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
        history_trigger=history_trigger,
    )


def _confirmed_match() -> CaseMemoryMatch:
    """构造一个带相似度的 confirmed 合成历史案例命中。

    CaseMemoryMatch 会再次验证状态，因此若夹具误改为 pending/rejected 会在工作流运行前失败；
    embedding 不进入夹具，符合公开历史上下文边界。
    """

    memory = CaseMemory(
        memory_id="mem_history_001",
        symptoms=["LTS 任务等待上游"],
        root_cause="上游数据未按时就绪",
        components=[Component.LTS],
        evidence_refs=["ev_history_001"],
        status=MemoryStatus.CONFIRMED,
        occurrence_count=2,
        created_at=NOW,
        updated_at=NOW,
    )
    return CaseMemoryMatch(memory=memory, similarity=0.94)


def _pending_stage_result() -> MemoryStageResult:
    """构造 accepted 报告产生的新 pending 记忆暂存结果。

    该对象只用于顶层编排测试，不模拟去重；MemoryStageResult 会验证 staged 必须带 memory、无
    duplicate 类型和相似度，从而保持与真实 CaseMemoryService 相同返回契约。
    """

    memory = CaseMemory(
        memory_id="mem_new_001",
        symptoms=["LTS 任务等待上游"],
        root_cause="上游数据未按时就绪",
        components=[Component.LTS],
        evidence_refs=["ev_realtime_001"],
        status=MemoryStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    return MemoryStageResult(status=MemoryStageStatus.STAGED, memory=memory)


@pytest.mark.asyncio
async def test_diagnosis_workflow_recalls_once_reuses_context_and_stages_accepted() -> None:
    """验证显式历史触发执行一次预算查询，并把同一 confirmed 案例贯穿两个子图。

    查询包含用户问题、实时 Observation 和假设但排除 CASE_MEMORY 文本；ReAct/报告请求收到同一
    案例，accepted 报告随后进入 staging。结果保留相似度、查询和三个阶段的公开契约。
    """

    react = RecordingReactWorkflow()
    report = RecordingReportWorkflow(ReportWorkflowOutcome.ACCEPTED)
    memory = RecordingMemoryWorkflow(
        matches=[_confirmed_match()],
        stage_result=_pending_stage_result(),
    )
    workflow = AuditedDiagnosisWorkflow(
        react=react,
        report=report,
        memory=memory,
        config=DiagnosisWorkflowConfig(memory_search_limit=2, memory_query_max_chars=512),
    )

    result = await workflow.run(
        DiagnosisRunRequest(
            state=_initial_state(),
            capability_request=_capability_request(HistoryTrigger.USER_REQUESTED),
        )
    )

    assert len(memory.search_calls) == 1
    query, limit = memory.search_calls[0]
    assert limit == 2
    assert len(query) <= 512
    assert "请检查 LTS 上游未就绪" in query
    assert "实时工具显示任务仍等待上游数据" in query
    assert "当前假设" in query
    assert "旧案例文本" not in query
    assert react.requests[0].confirmed_case_memories == (_confirmed_match().memory,)
    assert report.requests[0].confirmed_case_memories == (_confirmed_match().memory,)
    assert len(memory.stage_calls) == 1
    assert memory.stage_calls[0].state.run_id == result.report.state.run_id
    assert memory.stage_calls[0].state.memory_candidate is None
    assert result.recalled_memories[0].similarity == 0.94
    assert result.memory_stage.status is MemoryStageStatus.STAGED
    assert result.report.state.memory_candidate == result.memory_stage.memory


@pytest.mark.asyncio
async def test_diagnosis_workflow_skips_unrequested_history_and_records_degraded_skip() -> None:
    """验证 not_requested 不查询记忆，但 degraded 报告仍调用 staging 获取明确安全跳过结果。

    ReAct 与 Auditor 收到空案例集合；记忆服务返回 skipped_not_accepted，证明顶层没有提前分支吞掉
    审计结果，也没有把降级报告错误写成 pending 候选。
    """

    react = RecordingReactWorkflow()
    report = RecordingReportWorkflow(ReportWorkflowOutcome.DEGRADED)
    memory = RecordingMemoryWorkflow(
        matches=[_confirmed_match()],
        stage_result=MemoryStageResult(status=MemoryStageStatus.SKIPPED_NOT_ACCEPTED),
    )
    workflow = AuditedDiagnosisWorkflow(
        react=react,
        report=report,
        memory=memory,
        config=DiagnosisWorkflowConfig(),
    )

    result = await workflow.run(
        DiagnosisRunRequest(
            state=_initial_state(run_id="run_diagnosis_unit_002"),
            capability_request=_capability_request(HistoryTrigger.NOT_REQUESTED),
        )
    )

    assert memory.search_calls == []
    assert react.requests[0].confirmed_case_memories == ()
    assert report.requests[0].confirmed_case_memories == ()
    assert len(memory.stage_calls) == 1
    assert result.memory_query is None
    assert result.recalled_memories == ()
    assert result.memory_stage.status is MemoryStageStatus.SKIPPED_NOT_ACCEPTED
    assert result.report.state.memory_candidate is None


@pytest.mark.asyncio
async def test_diagnosis_workflow_propagates_memory_search_failure_before_agents() -> None:
    """验证历史查询依赖失败会终止顶层图，且 Planner/Auditor/staging 均不会继续执行。

    该语义防止数据库或 Embedding Provider 故障被伪装为“没有历史案例”；异常原样返回调用方，
    空调用记录证明尚未产生模型成本、MCP 副作用或不完整诊断结果。
    """

    react = RecordingReactWorkflow()
    report = RecordingReportWorkflow(ReportWorkflowOutcome.ACCEPTED)
    memory = RecordingMemoryWorkflow(
        matches=[],
        stage_result=_pending_stage_result(),
        search_error=RuntimeError("synthetic memory search failure"),
    )
    workflow = AuditedDiagnosisWorkflow(
        react=react,
        report=report,
        memory=memory,
        config=DiagnosisWorkflowConfig(),
    )

    with pytest.raises(RuntimeError, match="synthetic memory search failure"):
        await workflow.run(
            DiagnosisRunRequest(
                state=_initial_state(run_id="run_diagnosis_unit_003"),
                capability_request=_capability_request(HistoryTrigger.PLANNER_VALIDATION),
            )
        )

    assert react.requests == []
    assert report.requests == []
    assert memory.stage_calls == []


@pytest.mark.asyncio
async def test_diagnosis_result_rejects_degraded_report_with_successful_memory_write() -> None:
    """验证最终契约拒绝 degraded 报告与 staged 记忆的危险组合。

    测试故意配置不可信 memory 替身返回写入成功；四个节点虽依次完成，``DiagnosisRunResult`` 仍在
    API/评测可见前失败，证明顶层结果模型是审计门禁之外的最后一致性防线。
    """

    workflow = AuditedDiagnosisWorkflow(
        react=RecordingReactWorkflow(),
        report=RecordingReportWorkflow(ReportWorkflowOutcome.DEGRADED),
        memory=RecordingMemoryWorkflow(
            matches=[],
            stage_result=_pending_stage_result(),
        ),
        config=DiagnosisWorkflowConfig(),
    )

    with pytest.raises(ValueError, match="degraded report must skip memory"):
        await workflow.run(
            DiagnosisRunRequest(
                state=_initial_state(run_id="run_diagnosis_unit_004"),
                capability_request=_capability_request(HistoryTrigger.NOT_REQUESTED),
            )
        )
