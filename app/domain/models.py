"""排障状态、证据、假设、报告和案例记忆的领域模型。

这些 Pydantic 模型是未来 LangGraph 节点之间唯一允许传递的数据形态。模型刻意不包含
Thought 或 reasoning_process，从数据结构层阻止原始思维链进入日志、API 和长期记忆。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.planner import PlannerDecision
from app.domain.tooling import McpToolRequest, McpToolResponse, ToolName


class Component(StrEnum):
    """限定当前作品支持的三个合成 DataOps 组件标识。

    使用字符串枚举让值可直接进入 JSON、数据库和 Prompt，同时拒绝未批准组件扩张；新增组件
    必须同步产品契约、工具白名单、Fixture 与评测，不能靠任意字符串静默进入状态。
    """

    LTS = "lts"
    BDS = "bds"
    FLASHSYNC = "flashsync"


class EvidenceSourceType(StrEnum):
    """区分证据来自实时工具、知识节点、图路径还是已确认案例记忆。

    显式来源类型使 Auditor 能按可靠性和时效性审查引用，并保证历史案例不会伪装成本次实时
    Observation；字符串值也便于 API 与持久化层稳定序列化。
    """

    TOOL = "tool"
    KNOWLEDGE_NODE = "knowledge_node"
    GRAPH_PATH = "graph_path"
    CASE_MEMORY = "case_memory"


class Evidence(BaseModel):
    """表示一个可被假设、结论和建议引用的最小审计证据单元。

    模型保存稳定 ID、来源、观察时间、可靠性和结构化元数据，但不保存原始思维链。字段长度
    与可靠性范围在边界处限制异常载荷，`extra="forbid"` 防止未审计字段进入运行轨迹。
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=100)
    source_type: EvidenceSourceType
    source_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    observed_at: datetime
    reliability: float = Field(ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HypothesisStatus(StrEnum):
    """描述故障假设从候选到支持、拒绝或确认的有限状态集合。

    状态枚举防止 Planner 用自由文本创造无法审计的阶段，并允许后续工作流用明确分支处理
    证据增强和冲突；确认状态仍必须由有效 evidence_refs 支撑。
    """

    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class FaultHypothesis(BaseModel):
    """保存一个候选根因及其支持证据、反对证据和当前置信度。

    组件列表与证据引用只保存稳定 ID，实际证据集中存于 AgentState，避免复制内容后发生漂移。
    置信度限制在零到一之间，但数值本身不能替代状态转换和 Auditor 的证据审查。
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=100)
    symptom: str = Field(min_length=1, max_length=1000)
    candidate_root_cause: str = Field(min_length=1, max_length=1000)
    components: list[Component] = Field(min_length=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.CANDIDATE
    confidence: float = Field(default=0, ge=0, le=1)


class RootCauseConclusion(BaseModel):
    """表示最终报告中的一项根因结论及其强制证据引用。

    `evidence_refs` 至少一项，从 Schema 层阻止无来源结论进入报告；置信度表达剩余不确定性，
    调用方仍需检查每个引用确实存在并支持文本，而不能只验证字段非空。
    """

    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(min_length=1)


class FaultChainStep(BaseModel):
    """表示报告中一段必须能够追溯到现有证据的故障传播链路。

    旧的自由文本列表无法判断某一段链路是否有依据；本模型把描述与至少一个 evidence_id/path_id
    绑定，使确定性门禁和 Auditor 能逐段核对。模型不表达模型内部推理，只保存可公开结论。
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1)


class RiskLevel(StrEnum):
    """限定修复步骤可公开的低、中、高三档风险等级。

    统一枚举便于 UI 排序、Auditor 校验和 Golden Case 断言，避免“严重”“一般”等自由文本造成
    语义不一致；风险等级必须与前置条件、回滚和验证说明一起使用。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RemediationStep(BaseModel):
    """定义可按顺序执行、可验证且包含回滚语义的单个修复建议。

    模型不执行任何生产写操作，只描述人工操作计划；强制风险、回滚和验证字段是为了让求职
    演示体现变更安全意识，并让 Auditor 拒绝只有“重跑任务”而无保护条件的建议。
    """

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=2000)
    risk_level: RiskLevel
    evidence_refs: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    rollback: str = Field(min_length=1, max_length=2000)
    verification: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_high_risk_controls(self) -> RemediationStep:
        """保证高风险建议同时具备事实依据、前置检查和可执行回滚边界。

        低/中风险的只读检查可以没有证据引用，但 high 操作若缺少任一保护就会在报告构造阶段
        失败，而不是只依赖 Prompt 提醒 Auditor。rollback/verification 已由字段非空约束保证。
        """

        # 高风险操作对错误根因更敏感，因此必须先证明必要性，再说明执行前置条件。
        if self.risk_level is RiskLevel.HIGH:
            if not self.evidence_refs:
                raise ValueError("high-risk remediation requires evidence references")
            if not self.prerequisites:
                raise ValueError("high-risk remediation requires prerequisites")
        return self


class SimilarCaseReference(BaseModel):
    """描述本次诊断命中的一个已确认历史案例及其可解释差异。

    共同点和差异点帮助 Planner 避免把相似度当作等价性；`confirmed` 与证据引用保留来源状态，
    实时 Observation 与案例冲突时必须优先采用本次观察而不是复制旧结论。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=100)
    similarity: float = Field(ge=0, le=1)
    confirmed: bool
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    """定义面向 API 和 Auditor 的完整结构化排障报告契约。

    摘要、故障链、根因、证据、修复、风险、不确定性和相似案例分字段保存，使前端只负责渲染，
    也让测试能逐项验证证据完整性；模型不允许用一段不可解析文本替代这些边界。
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4000)
    fault_chain: list[FaultChainStep] = Field(default_factory=list)
    root_causes: list[RootCauseConclusion] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    remediation_steps: list[RemediationStep] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    similar_cases: list[SimilarCaseReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_remediation_order(self) -> DiagnosisReport:
        """要求修复步骤从一开始连续编号，避免 UI 与人工执行顺序产生歧义。

        空步骤列表用于证据不足的降级报告；非空列表必须严格为 1..N。该规则只校验结构顺序，
        不把自然语言动作当作已执行事实，具体引用和语义仍由确定性门禁与 Auditor 审核。
        """

        orders = [step.order for step in self.remediation_steps]
        if orders and orders != list(range(1, len(orders) + 1)):
            raise ValueError("remediation order must be consecutive starting at one")
        return self


class MemoryStatus(StrEnum):
    """表示长期案例记忆候选的待确认、已确认和已拒绝状态。

    默认待确认保证 Auditor 通过的报告仍不会自动污染默认召回；只有 confirmed 案例可参与常规
    历史匹配，rejected 记录可保留审计轨迹但不能被当作知识事实。
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class CaseMemory(BaseModel):
    """保存可跨会话复用且经过审计流程控制的结构化案例记忆。

    症状、根因、路径、方案和证据引用支持后续匹配，出现次数与时间戳支持去重更新而非重复插入。
    该模型只表达候选数据；写入、确认和冲突优先级仍由长期记忆服务与 Auditor 控制。
    """

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=100)
    symptoms: list[str] = Field(min_length=1)
    root_cause: str = Field(min_length=1, max_length=1000)
    fault_path: list[str] = Field(default_factory=list)
    solution_steps: list[str] = Field(default_factory=list)
    components: list[Component] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(min_length=1)
    status: MemoryStatus = MemoryStatus.PENDING
    occurrence_count: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class ToolEvent(BaseModel):
    """记录一次具体 MCP 调用尝试的请求、响应、时间与重试属性。

    每次尝试独立成事件，即使重试成功也保留首次失败，便于计算真实延迟和失败率。事件保存统一
    契约对象而非传输层原始消息，并通过时间校验保证审计时间线不会倒序。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=100)
    trace_id: str = Field(min_length=3, max_length=100)
    tool_name: ToolName
    request: McpToolRequest
    response: McpToolResponse
    attempt: int = Field(default=1, ge=1, le=2)
    retryable: bool = False
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_timing(self) -> ToolEvent:
        """校验事件时间戳带时区且完成时间不早于开始时间。

        时区是跨容器重放和日志聚合的必要条件；顺序约束防止错误时钟或组装 bug 产生负耗时。
        校验失败抛出 Pydantic ValueError，使无效事件不能进入 AgentState 或观测指标。
        """

        # 先检查时区，再比较先后；无时区 datetime 的比较虽可执行，却无法跨环境解释。
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("tool event timestamps must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        return self


class RetrievedPath(BaseModel):
    """表示 GraphRAG 返回的一条可追溯节点/关系路径及组合分数。

    路径至少包含两个节点和一条关系，source_ids 指向人工知识来源；稳定 path_id 允许报告引用
    并在删边消融中验证结果确实依赖图结构，而不是仅依赖相似文本。
    """

    model_config = ConfigDict(extra="forbid")

    path_id: str = Field(min_length=1, max_length=100)
    node_ids: list[str] = Field(min_length=2)
    relation_types: list[str] = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    source_ids: list[str] = Field(min_length=1)


class AuditStatus(StrEnum):
    """限定 Auditor 对报告只能接受或要求一次结构化返工。

    有限状态使 LangGraph 能建立确定性路由，并避免 Auditor 自行生成第三种含糊结果；返工预算
    由 Settings 限制，防止两个 Agent 无限互相修订。
    """

    ACCEPT = "accept"
    REVISE = "revise"


class AuditIssueCode(StrEnum):
    """枚举确定性规则和 Auditor 可以报告的有限审计问题类型。

    代码值让 LangGraph、测试和 API 无需解析自然语言；集合只覆盖引用、事实支撑、冲突、风险、
    案例状态、完整性与 Auditor 可用性，不允许模型借自定义代码扩张工作流分支。
    """

    INVALID_EVIDENCE_REF = "invalid_evidence_ref"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    EVIDENCE_CONFLICT = "evidence_conflict"
    MISSING_RISK_CONTROL = "missing_risk_control"
    UNCONFIRMED_CASE = "unconfirmed_case"
    REPORT_INCOMPLETE = "report_incomplete"
    AUDITOR_UNAVAILABLE = "auditor_unavailable"


class AuditIssue(BaseModel):
    """保存一条可公开、可定位且不能携带新事实的结构化审计问题。

    `claim_path` 使用报告字段路径定位问题，`evidence_refs` 只引用被审查的现有 ID；message 解释
    为什么不能放行，但不得提出新的根因。严格字段边界防止自由字典进入返工控制流。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: AuditIssueCode
    claim_path: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=1000)
    evidence_refs: tuple[str, ...] = ()


class AuditResult(BaseModel):
    """保存 Auditor 的接受/返工结论、问题列表和修订指令。

    Auditor 只能指出证据、矛盾或风险说明问题，不在此模型中注入新事实。结构化列表让 Planner
    能有界修订，也让测试验证无依据结论会被拦截。
    """

    model_config = ConfigDict(extra="forbid")

    status: AuditStatus
    issues: list[AuditIssue] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_payload(self) -> AuditResult:
        """绑定 accept/revise 与问题、返工指令，形成唯一可路由的组合。

        accept 必须没有问题或返工内容；revise 必须同时说明至少一个问题和一条修订指令。这样
        LangGraph 只读取枚举即可安全路由，不会出现“口头接受但仍列出阻断问题”的矛盾状态。
        """

        if self.status is AuditStatus.ACCEPT:
            if self.issues or self.revision_instructions:
                raise ValueError("accepted audit cannot contain issues or revision instructions")
            return self
        if not self.issues:
            raise ValueError("revise audit requires at least one issue")
        if not self.revision_instructions:
            raise ValueError("revise audit requires revision instructions")
        return self


class AgentState(BaseModel):
    """定义 LangGraph 全流程唯一共享的可序列化诊断状态。

    状态汇集计划、假设、证据、工具事件、图路径、报告、审计和记忆候选，但刻意不包含 Thought
    或 reasoning_process。所有节点通过该模型交换数据，从结构上阻止松散字典和原始思维链
    进入 checkpoint、日志或 API；额外字段会被拒绝以暴露契约漂移。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=100)
    user_query: str = Field(min_length=1, max_length=4000)
    intent: str | None = Field(default=None, max_length=100)
    active_capabilities: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    hypotheses: list[FaultHypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    retrieved_paths: list[RetrievedPath] = Field(default_factory=list)
    react_step: int = Field(default=0, ge=0)
    next_action: PlannerDecision | None = None
    observation_refs: list[str] = Field(default_factory=list)
    stop_reason: str | None = Field(default=None, max_length=500)
    draft_report: DiagnosisReport | None = None
    audit_result: AuditResult | None = None
    retry_count: int = Field(default=0, ge=0, le=1)
    memory_candidate: CaseMemory | None = None
