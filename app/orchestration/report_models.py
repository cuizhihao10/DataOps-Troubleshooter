"""定义报告草稿、Auditor 审计、一次返工和安全降级工作流的强类型状态。

这些模型与 Planner ReAct 循环分离，使已稳定的 Action/Observation 控制器保持单一职责；完整
诊断流程可顺序组合两段 LangGraph。事件只包含公开审计摘要和问题代码，不保存 Thought。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import CapabilitySelection
from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    AuditStatus,
    CaseMemory,
    MemoryStatus,
)
from app.retrieval.models import GraphEvidenceBundle

AUDITED_REPORT_WORKFLOW_CONTRACT_ID = "audited-report-workflow:v1"


class ReportWorkflowStatus(StrEnum):
    """区分报告图仍在执行还是已经形成可返回结果。

    具体是否通过审计由 outcome 表达，避免把 completed 与 accepted 混为一谈；有限两态让条件边
    易于穷举，也防止模型自由文本创建新控制状态。
    """

    RUNNING = "running"
    COMPLETED = "completed"


class ReportWorkflowOutcome(StrEnum):
    """表示最终报告被 Auditor 接受或因未通过而安全降级。

    degraded 不是成功审计，只说明系统已移除未经放行的根因和写操作建议并可安全返回；API/评测
    可以据此禁止长期记忆暂存和生产执行。
    """

    ACCEPTED = "accepted"
    DEGRADED = "degraded"


class ReportEventType(StrEnum):
    """限定报告工作流可以公开记录的四类结构化事件。

    事件覆盖草稿、审计、唯一返工和最终降级；不包含模型 Reason、原始输出或供应商响应体，适合
    进入运行时间线和演示 UI。
    """

    DRAFT_CREATED = "draft_created"
    AUDIT_COMPLETED = "audit_completed"
    REVISION_APPLIED = "revision_applied"
    SAFE_DEGRADED = "safe_degraded"


class ReportPublicEvent(BaseModel):
    """保存一条可公开的报告/审计事件及稳定问题代码。

    audit_status 只出现在审计/降级事件，revision_number 记录已执行报告级返工次数；issue_codes
    不复制模型消息，既可解释又避免把未验证自然语言当作事实。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^report_evt_[a-f0-9]{16}$")
    sequence: int = Field(ge=1)
    event_type: ReportEventType
    summary: str = Field(min_length=1, max_length=500)
    audit_status: AuditStatus | None = None
    issue_codes: tuple[AuditIssueCode, ...] = ()
    revision_number: int = Field(ge=0, le=1)


class ReportWorkflowConfig(BaseModel):
    """集中限制报告级返工最多为零或一次。

    该预算与 Auditor Schema 修复不同：前者重新生成报告并再次审计，后者只修复同一模型响应格式。
    模型冻结，运行中不能被 Agent 扩大。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_revisions: int = Field(default=1, ge=0, le=1)


class ReportRunRequest(BaseModel):
    """封装 Planner 停止后的状态、能力快照和可审计检索/案例上下文。

    请求要求 stop_reason 已存在，证明 ReAct 已进入终态；capabilities 必须与 AgentState 对齐，案例
    只能 confirmed。额外字段被拒绝，防止未审计自由数据进入报告 Prompt。
    """

    model_config = ConfigDict(extra="forbid")

    state: AgentState
    capabilities: CapabilitySelection
    evidence_bundle: GraphEvidenceBundle | None = None
    confirmed_case_memories: tuple[CaseMemory, ...] = ()

    @model_validator(mode="after")
    def validate_post_react_boundary(self) -> ReportRunRequest:
        """确认 ReAct 已停止、能力一致且历史案例全部经过确认。

        校验在构造 LangGraph 状态前完成；任一失败都不会调用 Builder 或 Auditor。retry_count 可以是
        零或一，但若已为一，工作流自然没有剩余返工预算。
        """

        if not self.state.stop_reason:
            raise ValueError("report workflow requires a completed ReAct state")
        expected_names = [name.value for name in self.capabilities.active_capabilities]
        if self.state.intent != self.capabilities.intent.value:
            raise ValueError("report workflow intent must match capability selection")
        if self.state.active_capabilities != expected_names:
            raise ValueError("report workflow capabilities must match state")
        if any(
            memory.status is not MemoryStatus.CONFIRMED for memory in self.confirmed_case_memories
        ):
            raise ValueError("report workflow can include only confirmed case memories")
        return self


class ReportGraphState(BaseModel):
    """保存报告 LangGraph 节点之间传递的完整可序列化状态。

    领域状态仍是唯一事实容器；图额外保存能力、检索上下文、确定性问题、公开事件和最终 outcome。
    不可序列化 Agent/Builder/Validator 通过 Runtime context 注入。
    """

    model_config = ConfigDict(extra="forbid")

    agent_state: AgentState
    capabilities: CapabilitySelection
    evidence_bundle: GraphEvidenceBundle | None = None
    confirmed_case_memories: tuple[CaseMemory, ...] = ()
    max_revisions: int = Field(default=1, ge=0, le=1)
    deterministic_issues: tuple[AuditIssue, ...] = ()
    events: list[ReportPublicEvent] = Field(default_factory=list)
    status: ReportWorkflowStatus = ReportWorkflowStatus.RUNNING
    outcome: ReportWorkflowOutcome | None = None


class ReportRunResult(BaseModel):
    """返回最终 AgentState、审计 outcome 和公开报告时间线。

    accepted 结果必须包含 AuditStatus.accept；degraded 结果保留 revise 或 Auditor 不可用问题，且
    draft_report 已被安全收窄。所有结果都要求至少草稿和终态事件，便于 API 解释控制流。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(pattern=r"^audited-report-workflow:v\d+$")
    state: AgentState
    outcome: ReportWorkflowOutcome
    events: list[ReportPublicEvent] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_terminal_result(self) -> ReportRunResult:
        """保证最终结果一定含报告、审计结论和正确终态事件。

        accepted 与 AuditStatus.accept 严格绑定；degraded 不得伪装为 accept。最后事件必须是审计完成
        或安全降级，防止图因边配置错误在修订中途静默结束。
        """

        if self.state.draft_report is None or self.state.audit_result is None:
            raise ValueError("completed report workflow requires report and audit result")
        if self.outcome is ReportWorkflowOutcome.ACCEPTED:
            if self.state.audit_result.status is not AuditStatus.ACCEPT:
                raise ValueError("accepted outcome requires accepted audit")
        elif self.state.audit_result.status is AuditStatus.ACCEPT:
            raise ValueError("degraded outcome cannot contain accepted audit")
        if self.events[-1].event_type not in {
            ReportEventType.AUDIT_COMPLETED,
            ReportEventType.SAFE_DEGRADED,
        }:
            raise ValueError("report workflow requires a terminal public event")
        return self
