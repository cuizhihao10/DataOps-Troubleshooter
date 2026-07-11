"""定义端到端诊断编排的请求、内部状态、配置和最终结果契约。

顶层编排只组合历史召回、已有 ReAct 子图、已有 Auditor 报告子图和长期记忆暂存，不创建第三个
Agent。Pydantic 模型把每段结果和 run/session ID 绑定，防止跨运行状态被错误拼接。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capabilities import CapabilitySelectionRequest, HistoryTrigger
from app.domain.models import AgentState
from app.memory.models import CaseMemoryMatch, MemoryStageResult, MemoryStageStatus
from app.orchestration.models import ReactRunResult
from app.orchestration.report_models import ReportRunResult, ReportWorkflowOutcome
from app.retrieval.models import GraphEvidenceBundle

DIAGNOSIS_WORKFLOW_CONTRACT_ID = "audited-diagnosis-workflow:v1"


class DiagnosisWorkflowStatus(StrEnum):
    """区分顶层诊断图仍在执行还是已经完成全部确定性收尾步骤。

    状态只保留 running/completed 两值；Planner 和 Auditor 的具体结束语义继续由各自结果模型表达，
    避免顶层工作流复制或重新解释子图状态。
    """

    RUNNING = "running"
    COMPLETED = "completed"


class DiagnosisWorkflowConfig(BaseModel):
    """集中限制历史案例召回数量和确定性查询文本预算。

    ``memory_search_limit`` 控制进入 Planner/Auditor 的 confirmed 案例数量；
    ``memory_query_max_chars`` 限制用户问题、实时 Observation 和假设拼接后的字符数。模型冻结，
    运行中不能扩大上下文预算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_search_limit: int = Field(default=5, ge=1, le=20)
    memory_query_max_chars: int = Field(default=4000, ge=256, le=20_000)


class DiagnosisRunRequest(BaseModel):
    """封装一次顶层诊断所需的初始状态、能力路由输入和可选 GraphRAG Bundle。

    ``state`` 可以携带会话恢复得到的实时 Evidence/假设，但用户问题必须含非空白字符。历史是否
    查询由 ``capability_request.history_trigger`` 决定，调用方不能直接注入未确认案例绕过仓储。
    """

    model_config = ConfigDict(extra="forbid")

    state: AgentState
    capability_request: CapabilitySelectionRequest
    evidence_bundle: GraphEvidenceBundle | None = None

    @model_validator(mode="after")
    def validate_user_query(self) -> DiagnosisRunRequest:
        """拒绝纯空白用户问题，使记忆查询和 Planner 输入共享同一明确失败边界。

        AgentState 的长度约束允许空格字符串，本层进一步要求至少一个可见字符；失败发生在任何
        数据库、模型或 MCP 调用前，不消耗外部资源，也不会产生只有 run ID 的空诊断。
        """

        if not self.state.user_query.strip():
            raise ValueError("diagnosis user query must not be blank")
        return self


class DiagnosisGraphState(BaseModel):
    """保存顶层 LangGraph 四个阶段之间传递的可序列化状态。

    状态逐步增加 memory query/matches、ReAct 结果、报告结果和 staging 结果；外部运行时对象通过
    LangGraph context 注入。可空字段只表示对应节点尚未完成，最终结果构造会拒绝缺失阶段。
    """

    model_config = ConfigDict(extra="forbid")

    initial_state: AgentState
    capability_request: CapabilitySelectionRequest
    evidence_bundle: GraphEvidenceBundle | None = None
    memory_query: str | None = None
    recalled_memories: tuple[CaseMemoryMatch, ...] = ()
    react_result: ReactRunResult | None = None
    report_result: ReportRunResult | None = None
    memory_stage: MemoryStageResult | None = None
    status: DiagnosisWorkflowStatus = DiagnosisWorkflowStatus.RUNNING


class DiagnosisRunResult(BaseModel):
    """返回历史召回、Planner 时间线、Auditor 报告和记忆暂存组成的完整诊断结果。

    结果保留 history trigger 与实际查询文本，便于 API/评测解释为什么发生或跳过案例召回；所有
    子结果必须属于同一 run/session，且报告 outcome 与 memory staging 跳过语义必须一致。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(pattern=r"^audited-diagnosis-workflow:v\d+$")
    history_trigger: HistoryTrigger
    memory_query: str | None = None
    recalled_memories: tuple[CaseMemoryMatch, ...] = ()
    react: ReactRunResult
    report: ReportRunResult
    memory_stage: MemoryStageResult

    @model_validator(mode="after")
    def validate_cross_stage_consistency(self) -> DiagnosisRunResult:
        """校验历史触发、run/session 身份和审计结果与记忆暂存状态的一致性。

        not_requested 必须没有查询或命中；其余触发必须记录查询。ReAct 与报告 run/session 不一致
        表示调用方拼接了不同诊断。degraded 必须由记忆层返回 skipped_not_accepted，accepted 则
        只能新增、合并或因无根因安全跳过，不能伪装为未通过审计。
        """

        if self.history_trigger is HistoryTrigger.NOT_REQUESTED:
            if self.memory_query is not None or self.recalled_memories:
                raise ValueError("unrequested history cannot contain a query or recalled memories")
        elif self.memory_query is None or not self.memory_query.strip():
            raise ValueError("requested history requires a recorded memory query")

        if self.react.state.run_id != self.report.state.run_id:
            raise ValueError("React and report results must share run_id")
        if self.react.state.session_id != self.report.state.session_id:
            raise ValueError("React and report results must share session_id")
        if self.report.state.memory_candidate != self.memory_stage.memory:
            raise ValueError("report state memory_candidate must match memory stage result")

        if self.report.outcome is ReportWorkflowOutcome.DEGRADED:
            if self.memory_stage.status is not MemoryStageStatus.SKIPPED_NOT_ACCEPTED:
                raise ValueError("degraded report must skip memory as not accepted")
        elif self.memory_stage.status is MemoryStageStatus.SKIPPED_NOT_ACCEPTED:
            raise ValueError("accepted report cannot use not-accepted memory skip status")
        return self
