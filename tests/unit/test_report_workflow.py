"""验证 LangGraph 报告草稿、确定性否决、独立审计、一次返工和安全降级。

测试使用结构化 Auditor/Builder 替身，不模拟自然语言解析；重点证明错误 accept 不能越过规则、
返工最多一次、二次 revise 或 Provider 失败不会放行根因或长期记忆。
"""

from datetime import UTC, datetime

import pytest

from app.agents.auditor import AuditorProviderError, AuditorTurnContext
from app.capabilities import (
    CapabilitySelection,
    CapabilitySelectionRequest,
    DiagnosisIntent,
    get_capability_registry,
)
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
    RemediationStep,
    RiskLevel,
    RootCauseConclusion,
)
from app.orchestration import (
    AuditedReportWorkflow,
    ReportEventType,
    ReportRunRequest,
    ReportWorkflowConfig,
    ReportWorkflowOutcome,
)

OBSERVED_AT = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


class ScriptedAuditor:
    """按预设顺序返回 AuditResult 或抛异常，并保存每轮强类型上下文。

    序列耗尽显式失败，避免工作流多调用 Auditor 却被默认 accept 掩盖；替身不修改报告或执行 I/O。
    """

    def __init__(self, outcomes: list[AuditResult | Exception]) -> None:
        """复制预设结果并初始化空上下文记录，保证测试输入列表不被就地消费。

        构造不调用 Prompt 或模型；每个测试实例独立，避免并发状态共享。序列耗尽由 review 抛出
        AssertionError，使意外的第二次返工或额外审计可见。
        """

        self._outcomes = list(outcomes)
        self.contexts: list[AuditorTurnContext] = []

    async def review(self, context: AuditorTurnContext) -> AuditResult:
        """记录上下文并消费一个预设结果，异常按原类型传播。

        若调用次数超出序列则抛 AssertionError，直接暴露返工边或终止条件配置错误；合法结果已经
        通过 Pydantic，测试不在此重复解析 JSON。
        """

        self.contexts.append(context)
        if not self._outcomes:
            raise AssertionError("Auditor was called more times than expected")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class UnsupportedReportBuilder:
    """故意生成引用存在但根因文本不对应任何假设的报告。

    该替身只用于证明确定性 Validator 能否决错误模型 accept；它仍返回通过 Pydantic 的报告，避免
    测试依靠无效 Schema 获得假阳性。
    """

    def build(
        self,
        state: AgentState,
        *,
        evidence_bundle=None,
        confirmed_case_memories=(),
        history_case_matches=(),
    ) -> DiagnosisReport:
        """使用状态中的真实 evidence_id 组装一个语义无依据的高置信度根因。

        可选参数保持生产 Builder 协议，但不读取检索/案例；输入缺 Evidence 时显式索引失败，保证
        测试不会悄悄退化为悬空引用场景。
        """

        evidence_ref = state.evidence[0].evidence_id
        return DiagnosisReport(
            summary="故意注入的无依据报告。",
            root_causes=[
                RootCauseConclusion(
                    root_cause="未在假设中出现的数据库损坏",
                    confidence=0.99,
                    evidence_refs=[evidence_ref],
                )
            ],
            evidence_refs=[evidence_ref],
            remediation_steps=[_readonly_step()],
            risks=["仅只读核验。"],
        )


def _selection() -> CapabilitySelection:
    """通过真实固定 registry 选择 LTS 单组件能力组合。

    返回对象用于请求和 AuditorTurnContext 一致性校验，避免测试伪造 capability 名称或遗漏风险/
    结构化报告能力。
    """

    return get_capability_registry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        )
    )


def _state(selection: CapabilitySelection) -> AgentState:
    """构造 ReAct 已停止且含一条实时 Evidence 的最小报告工作流输入。

    状态不含 supported 假设，因此生产 Builder 会安全降级；UnsupportedBuilder 则可用同一真实引用
    注入语义错误根因。能力字段与 selection 精确对齐。
    """

    return AgentState(
        run_id="run_report_workflow_001",
        session_id="session_report_workflow_001",
        user_query="检查 LTS 合成任务",
        intent=selection.intent.value,
        active_capabilities=[name.value for name in selection.active_capabilities],
        evidence=[
            Evidence(
                evidence_id="ev_report_workflow_001",
                source_type=EvidenceSourceType.TOOL,
                source_id="synthetic_lts_status",
                content="合成任务状态为等待上游。",
                observed_at=OBSERVED_AT,
                reliability=0.95,
            )
        ],
        stop_reason="evidence_insufficient",
    )


def _request() -> ReportRunRequest:
    """构造能力与 AgentState 对齐的后 ReAct 报告请求。

    不含 GraphRAG/历史案例，使控制流测试只关注审计；请求仍通过全部 post-ReAct 边界校验。
    """

    selection = _selection()
    return ReportRunRequest(state=_state(selection), capabilities=selection)


def _readonly_step() -> RemediationStep:
    """构造低风险、具备前置/回滚/验证的只读步骤。

    步骤用于注入报告，不触发风险门禁，确保测试问题只来自 unsupported root cause；它不引用
    根因证据，也不声称执行过生产操作。
    """

    return RemediationStep(
        order=1,
        action="继续只读核验。",
        risk_level=RiskLevel.LOW,
        prerequisites=["确认 run_id。"],
        rollback="不修改系统状态。",
        verification="记录 Evidence。",
    )


def _accept() -> AuditResult:
    """返回一个不带问题和指令的合法 Auditor accept。

    该结果可能被确定性门禁否决，测试据此证明模型没有最终放行权；返回对象不包含任何业务事实，
    也不会修改待审计报告。
    """

    return AuditResult(status=AuditStatus.ACCEPT)


def _revise(code: AuditIssueCode = AuditIssueCode.UNSUPPORTED_CLAIM) -> AuditResult:
    """构造包含单个有限问题和修订指令的合法 revise 结果。

    默认问题不新增事实，只表示报告声明未被支持；可覆盖 code 测试其他安全路径。输出通过
    AuditResult 的 revise 跨字段校验，不依赖自由文本控制流。
    """

    issue = AuditIssue(
        code=code,
        claim_path="root_causes[0]",
        message="合成审计要求删除未支持结论。",
    )
    return AuditResult(
        status=AuditStatus.REVISE,
        issues=[issue],
        revision_instructions=["删除未支持结论并明确不确定性。"],
    )


@pytest.mark.asyncio
async def test_valid_draft_is_accepted_without_revision() -> None:
    """验证生产 Builder 的证据不足降级草稿可被 Auditor 一次接受。

    outcome accepted、retry_count=0 和事件 draft/audit 证明没有无意义返工；最终报告无根因且保留
    uncertainties/只读步骤，accept 不等于虚构完整答案。
    """

    auditor = ScriptedAuditor([_accept()])
    workflow = AuditedReportWorkflow(
        auditor=auditor,
        config=ReportWorkflowConfig(max_revisions=1),
    )

    result = await workflow.run(_request())

    assert result.outcome is ReportWorkflowOutcome.ACCEPTED
    assert result.state.retry_count == 0
    assert result.state.audit_result is not None
    assert result.state.audit_result.status is AuditStatus.ACCEPT
    assert result.state.draft_report is not None
    assert result.state.draft_report.root_causes == []
    assert [event.event_type for event in result.events] == [
        ReportEventType.DRAFT_CREATED,
        ReportEventType.AUDIT_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_deterministic_policy_vetoes_model_accept_then_allows_one_safe_revision() -> None:
    """验证错误 accept 被规则否决，唯一修订删除根因后才可第二次接受。

    首轮上下文必须包含 unsupported_claim；第二轮问题为空。事件序列含且只含一次 revision，最终
    retry_count=1、根因为空，证明返工没有改写另一个事实。
    """

    auditor = ScriptedAuditor([_accept(), _accept()])
    workflow = AuditedReportWorkflow(
        auditor=auditor,
        config=ReportWorkflowConfig(max_revisions=1),
        builder=UnsupportedReportBuilder(),
    )

    result = await workflow.run(_request())

    assert result.outcome is ReportWorkflowOutcome.ACCEPTED
    assert result.state.retry_count == 1
    assert result.state.draft_report is not None
    assert result.state.draft_report.root_causes == []
    assert [issue.code for issue in auditor.contexts[0].deterministic_issues] == [
        AuditIssueCode.UNSUPPORTED_CLAIM
    ]
    assert auditor.contexts[1].deterministic_issues == ()
    assert [event.event_type for event in result.events] == [
        ReportEventType.DRAFT_CREATED,
        ReportEventType.AUDIT_COMPLETED,
        ReportEventType.REVISION_APPLIED,
        ReportEventType.AUDIT_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_second_revise_degrades_and_removes_unaccepted_claims() -> None:
    """验证两轮 Auditor 都 revise 时不发生第二次返工，而是形成 degraded 终态。

    Auditor 恰好调用两次、revision 事件恰好一次；最终报告没有根因/链路且含禁止生产写操作风险，
    audit_result 保留 revise 以明确未通过。
    """

    auditor = ScriptedAuditor([_revise(), _revise()])
    workflow = AuditedReportWorkflow(
        auditor=auditor,
        config=ReportWorkflowConfig(max_revisions=1),
        builder=UnsupportedReportBuilder(),
    )

    result = await workflow.run(_request())

    assert result.outcome is ReportWorkflowOutcome.DEGRADED
    assert len(auditor.contexts) == 2
    assert result.state.retry_count == 1
    assert result.state.audit_result is not None
    assert result.state.audit_result.status is AuditStatus.REVISE
    assert result.state.draft_report is not None
    assert result.state.draft_report.root_causes == []
    assert result.events[-1].event_type is ReportEventType.SAFE_DEGRADED


@pytest.mark.asyncio
async def test_auditor_provider_failure_degrades_without_report_revision_retry() -> None:
    """验证 Auditor 连接失败不会放行或浪费报告返工预算。

    Provider 错误直接生成 auditor_unavailable issue 和安全降级稿；Auditor 只调用一次、retry_count
    保持零，事件只有 draft/degraded，异常公开摘要不包含 URL 或凭据。
    """

    error = AuditorProviderError(
        error_code="connection_error",
        public_summary="无法连接合成 Auditor 服务。",
        retryable=True,
    )
    auditor = ScriptedAuditor([error])
    workflow = AuditedReportWorkflow(
        auditor=auditor,
        config=ReportWorkflowConfig(max_revisions=1),
    )

    result = await workflow.run(_request())

    assert result.outcome is ReportWorkflowOutcome.DEGRADED
    assert result.state.retry_count == 0
    assert result.state.audit_result is not None
    assert result.state.audit_result.issues[0].code is AuditIssueCode.AUDITOR_UNAVAILABLE
    assert [event.event_type for event in result.events] == [
        ReportEventType.DRAFT_CREATED,
        ReportEventType.SAFE_DEGRADED,
    ]


@pytest.mark.asyncio
async def test_zero_revision_budget_degrades_after_first_revise_without_incrementing_counter() -> (
    None
):
    """验证 max_revisions=0 时首次 revise 直接降级且不伪装执行过返工。

    Auditor 只调用一次，retry_count 保持零，事件中没有 REVISION_APPLIED；该边界证明配置可以完全
    关闭报告返工，同时仍要求 Auditor 审查并安全删除未放行内容。
    """

    auditor = ScriptedAuditor([_revise()])
    workflow = AuditedReportWorkflow(
        auditor=auditor,
        config=ReportWorkflowConfig(max_revisions=0),
    )

    result = await workflow.run(_request())

    assert result.outcome is ReportWorkflowOutcome.DEGRADED
    assert len(auditor.contexts) == 1
    assert result.state.retry_count == 0
    assert ReportEventType.REVISION_APPLIED not in {event.event_type for event in result.events}
    assert result.events[-1].event_type is ReportEventType.SAFE_DEGRADED
