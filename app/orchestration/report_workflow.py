"""用 LangGraph 实现确定性草稿、独立 Auditor、一次返工和安全降级。

工作流接收已经停止的 Planner ReAct 状态。草稿/规则/修订均为确定性服务，只有 Auditor 是第二个
LLM Agent；任何规则问题拥有否决权，Auditor 不可用或二次未通过时都不会放行长期记忆。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.agents.auditor import AuditorAgent, AuditorAgentError, AuditorTurnContext
from app.domain.models import (
    AgentState,
    AuditIssue,
    AuditIssueCode,
    AuditResult,
    AuditStatus,
    CaseMemory,
    DiagnosisReport,
)
from app.orchestration.report_models import (
    AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
    ReportEventType,
    ReportGraphState,
    ReportPublicEvent,
    ReportRunRequest,
    ReportRunResult,
    ReportWorkflowConfig,
    ReportWorkflowOutcome,
    ReportWorkflowStatus,
)
from app.reporting import (
    DeterministicReportBuilder,
    ReportPolicyValidator,
    SafeReportReviser,
)
from app.retrieval.models import GraphEvidenceBundle


class ReportDraftBuilder(Protocol):
    """声明报告工作流需要的最小确定性草稿接口。

    生产实现和测试替身都必须只返回 DiagnosisReport，不修改 AgentState 或调用模型；协议便于测试
    故意注入无依据报告，证明确定性门禁能否决错误的 Auditor accept。
    """

    def build(
        self,
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
    ) -> DiagnosisReport:
        """从已停止调查状态创建首版结构化报告。

        输入仅为强类型事实上下文，输出必须通过 DiagnosisReport Schema；实现不得写 memory、执行
        修复或返回自由文本。
        """

        ...


class ReportValidator(Protocol):
    """声明规则门禁所需的只读报告校验接口。

    校验器返回零到多条 AuditIssue，不修改报告；空元组表示客观不变量通过，但仍需 Auditor 做
    语义审查。协议不提供修订方法，因此校验阶段不能偷偷改变待审核对象或返工次数。
    """

    def validate(
        self,
        report: DiagnosisReport,
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
    ) -> tuple[AuditIssue, ...]:
        """检查引用、假设支撑、冲突、风险和案例状态并返回结构化问题。

        实现不得把模型 accept 当作豁免，也不得抛弃已发现的问题；编程/契约错误应显式抛出，
        空元组只能表示本轮客观规则全部通过。
        """

        ...


class ReportReviser(Protocol):
    """声明一次保守修订与最终安全降级的确定性接口。

    两个方法都只能收窄报告，不能新增根因或执行工具；工作流根据剩余返工预算选择其一。协议
    不接收 Auditor 自由文本之外的新事实来源，从类型上限制修订能力。
    """

    def revise(
        self,
        report: DiagnosisReport,
        issues: tuple[AuditIssue, ...],
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
    ) -> DiagnosisReport:
        """根据首次结构化问题创建仍需再次审计的收窄报告。

        输出不代表通过，调用方必须再次执行 Validator 和 Auditor；实现不得增加 retry_count。
        """

        ...

    def degrade(
        self,
        report: DiagnosisReport,
        issues: tuple[AuditIssue, ...],
        state: AgentState,
        *,
        evidence_bundle: GraphEvidenceBundle | None = None,
        confirmed_case_memories: tuple[CaseMemory, ...] = (),
    ) -> DiagnosisReport:
        """生成不含未审计结论的最终安全降级报告。

        输出可返回用户但不能触发长期记忆或生产修复；实现必须保留明确 uncertainties，并删除
        所有未获独立审计接受的根因、链路和案例结论。
        """

        ...


@dataclass(frozen=True, slots=True)
class ReportGraphRuntime:
    """保存报告图共享但不进入 checkpoint 的 Agent、服务和预算。

    所有依赖只读或按调用返回新模型；每个 workflow 实例可以复用编译图，单次运行的可变数据全部
    位于 ReportGraphState，避免并发会话相互污染。
    """

    auditor: AuditorAgent
    builder: ReportDraftBuilder
    validator: ReportValidator
    reviser: ReportReviser
    config: ReportWorkflowConfig


class AuditedReportWorkflow:
    """编译并运行 draft → audit → revise once/degrade 的固定 LangGraph。

    构造注入独立 Auditor 和确定性服务；`run` 从 Planner 终态开始，最终返回 accepted 或 degraded。
    图不会重新执行 MCP，报告级返工若需要新证据只能降级，后续切片再接回 Planner 调查边。
    """

    def __init__(
        self,
        *,
        auditor: AuditorAgent,
        config: ReportWorkflowConfig,
        builder: ReportDraftBuilder | None = None,
        validator: ReportValidator | None = None,
        reviser: ReportReviser | None = None,
    ) -> None:
        """保存依赖并一次编译固定报告图，不调用模型或外部系统。

        缺省使用生产确定性实现；测试可注入严格替身。返工预算由 Pydantic config 限制为零或一，
        不允许节点用魔法数字扩张循环。
        """

        self._runtime = ReportGraphRuntime(
            auditor=auditor,
            builder=builder or DeterministicReportBuilder(),
            validator=validator or ReportPolicyValidator(),
            reviser=reviser or SafeReportReviser(),
            config=config,
        )
        self._graph = _build_report_graph()

    async def run(self, request: ReportRunRequest) -> ReportRunResult:
        """执行报告工作流并返回有审计 outcome 的最终状态与公开事件。

        请求已在 Pydantic 层验证 ReAct 终态和 confirmed memories；LangGraph recursion_limit 只覆盖
        草稿、两次审计、一次修订和降级。预期 Auditor 失败在节点内安全降级，编程错误继续传播。
        """

        initial_state = ReportGraphState(
            agent_state=request.state,
            capabilities=request.capabilities,
            evidence_bundle=request.evidence_bundle,
            confirmed_case_memories=request.confirmed_case_memories,
            max_revisions=self._runtime.config.max_revisions,
        )
        raw_state = await self._graph.ainvoke(
            initial_state,
            context=self._runtime,
            config={"recursion_limit": 10},
        )
        final_state = ReportGraphState.model_validate(raw_state)
        if final_state.status is not ReportWorkflowStatus.COMPLETED:
            raise RuntimeError("report graph ended without completed status")
        if final_state.outcome is None:
            raise RuntimeError("completed report graph requires outcome")
        return ReportRunResult(
            contract_id=AUDITED_REPORT_WORKFLOW_CONTRACT_ID,
            state=final_state.agent_state,
            outcome=final_state.outcome,
            events=final_state.events,
        )


def _build_report_graph():
    """构建草稿、审计、条件返工和降级的固定 LangGraph 拓扑。

    条件边只读取 AuditStatus、retry_count 与 outcome，不解析问题文本。编译在构造期完成，任何节点
    名称或边配置错误不会延迟到首个用户请求。
    """

    graph = StateGraph(ReportGraphState, context_schema=ReportGraphRuntime)
    graph.add_node("draft_report", _draft_report)
    graph.add_node("audit_report", _audit_report)
    graph.add_node("revise_report", _revise_report)
    graph.add_node("degrade_report", _degrade_report)
    graph.add_edge(START, "draft_report")
    graph.add_edge("draft_report", "audit_report")
    graph.add_conditional_edges(
        "audit_report",
        _route_after_audit,
        {"revise": "revise_report", "degrade": "degrade_report", "end": END},
    )
    graph.add_edge("revise_report", "audit_report")
    graph.add_edge("degrade_report", END)
    return graph.compile(name="dataops_audited_report_v1")


async def _draft_report(
    graph_state: ReportGraphState,
    runtime: Runtime[ReportGraphRuntime],
) -> ReportGraphState:
    """调用确定性 Builder 创建草稿并执行首次客观规则预检。

    报告和 issues 在一个 state copy 中原子写入，Auditor 不会看到“有报告但规则未计算”的中间态。
    Builder/Validator 异常属于实现缺陷并传播，不能伪装为模型拒绝或安全降级。
    """

    # 先完成纯投影，再运行规则校验；Auditor 永远不会看到缺少预检结果的半成品草稿。
    report = runtime.context.builder.build(
        graph_state.agent_state,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
    )
    issues = runtime.context.validator.validate(
        report,
        graph_state.agent_state,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
    )
    agent_state = graph_state.agent_state.model_copy(
        update={"draft_report": report, "audit_result": None}
    )
    updated = graph_state.model_copy(
        update={"agent_state": agent_state, "deterministic_issues": issues}
    )
    return _append_report_event(
        updated,
        event_type=ReportEventType.DRAFT_CREATED,
        summary=f"确定性草稿已生成，规则预检发现 {len(issues)} 个问题。",
    )


async def _audit_report(
    graph_state: ReportGraphState,
    runtime: Runtime[ReportGraphRuntime],
) -> ReportGraphState:
    """调用独立 Auditor，并让确定性问题对模型 accept 拥有最终否决权。

    每轮先基于当前修订稿重新计算规则问题，再构造 AuditorTurnContext。预期 Provider/refusal/Schema
    失败不重跑报告，而是立即生成安全降级稿；合法结果与规则问题合并后写回 AgentState。
    """

    report = graph_state.agent_state.draft_report
    if report is None:
        raise RuntimeError("audit node requires a draft report")
    # 每轮审计都重新校验当前草稿，不能沿用首次 issues 误判修订后的引用状态。
    deterministic_issues = runtime.context.validator.validate(
        report,
        graph_state.agent_state,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
    )
    context = AuditorTurnContext(
        state=graph_state.agent_state,
        capabilities=graph_state.capabilities,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
        deterministic_issues=deterministic_issues,
        revision_number=graph_state.agent_state.retry_count,
    )
    try:
        model_result = await runtime.context.auditor.review(context)
    except AuditorAgentError as exc:
        unavailable_issue = AuditIssue(
            code=AuditIssueCode.AUDITOR_UNAVAILABLE,
            claim_path="auditor",
            message=exc.public_summary,
        )
        audit_result = AuditResult(
            status=AuditStatus.REVISE,
            issues=[unavailable_issue],
            revision_instructions=["Auditor 不可用，禁止放行并返回安全降级报告。"],
        )
        degraded = runtime.context.reviser.degrade(
            report,
            tuple(audit_result.issues),
            graph_state.agent_state,
            evidence_bundle=graph_state.evidence_bundle,
            confirmed_case_memories=graph_state.confirmed_case_memories,
        )
        agent_state = graph_state.agent_state.model_copy(
            update={"draft_report": degraded, "audit_result": audit_result}
        )
        stopped = graph_state.model_copy(
            update={
                "agent_state": agent_state,
                "deterministic_issues": tuple(audit_result.issues),
                "status": ReportWorkflowStatus.COMPLETED,
                "outcome": ReportWorkflowOutcome.DEGRADED,
            }
        )
        return _append_report_event(
            stopped,
            event_type=ReportEventType.SAFE_DEGRADED,
            summary=f"Auditor 失败（{exc.stop_reason}），报告未放行并已安全降级。",
            audit_status=AuditStatus.REVISE,
            issues=tuple(audit_result.issues),
        )

    # 模型只能补充语义问题，确定性问题始终排在前面并可把错误 accept 强制改为 revise。
    audit_result = _merge_audit_result(model_result, deterministic_issues)
    agent_state = graph_state.agent_state.model_copy(update={"audit_result": audit_result})
    if audit_result.status is AuditStatus.ACCEPT:
        updated = graph_state.model_copy(
            update={
                "agent_state": agent_state,
                "deterministic_issues": (),
                "status": ReportWorkflowStatus.COMPLETED,
                "outcome": ReportWorkflowOutcome.ACCEPTED,
            }
        )
    else:
        updated = graph_state.model_copy(
            update={
                "agent_state": agent_state,
                "deterministic_issues": tuple(audit_result.issues),
            }
        )
    return _append_report_event(
        updated,
        event_type=ReportEventType.AUDIT_COMPLETED,
        summary=(
            "Auditor 接受报告。"
            if audit_result.status is AuditStatus.ACCEPT
            else f"Auditor 要求修订，记录 {len(audit_result.issues)} 个问题。"
        ),
        audit_status=audit_result.status,
        issues=tuple(audit_result.issues),
    )


def _route_after_audit(graph_state: ReportGraphState) -> str:
    """根据结构化 outcome、AuditStatus 和剩余预算选择结束、返工或降级。

    Auditor 异常路径已设置 completed/degraded，直接结束；accept 也结束。revise 只有在 retry_count
    小于配置预算时才能进入返工，否则进入降级。函数不读取 revision instruction 自然语言。
    """

    if graph_state.status is ReportWorkflowStatus.COMPLETED:
        return "end"
    audit_result = graph_state.agent_state.audit_result
    if audit_result is None:
        raise RuntimeError("audit routing requires AuditResult")
    if audit_result.status is AuditStatus.ACCEPT:
        raise RuntimeError("accepted audit must mark report workflow completed")
    return (
        "revise" if graph_state.agent_state.retry_count < graph_state.max_revisions else "degrade"
    )


async def _revise_report(
    graph_state: ReportGraphState,
    runtime: Runtime[ReportGraphRuntime],
) -> ReportGraphState:
    """消费首次 AuditIssue，执行唯一一次确定性收窄并清空旧审计结果。

    max_revisions=0 时不调用 Reviser，直接由本节点生成降级准备状态；正常路径 retry_count 原子加一，
    新草稿随后必须再次经过 Validator 和 Auditor，不能沿用旧 accept/revise。
    """

    report = graph_state.agent_state.draft_report
    audit_result = graph_state.agent_state.audit_result
    if report is None or audit_result is None or audit_result.status is not AuditStatus.REVISE:
        raise RuntimeError("revision node requires a revise audit and draft report")
    if graph_state.agent_state.retry_count >= runtime.context.config.max_revisions:
        raise RuntimeError("revision node entered after revision budget exhausted")
    revised = runtime.context.reviser.revise(
        report,
        tuple(audit_result.issues),
        graph_state.agent_state,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
    )
    agent_state = graph_state.agent_state.model_copy(
        update={
            "draft_report": revised,
            "audit_result": None,
            "retry_count": graph_state.agent_state.retry_count + 1,
        }
    )
    updated = graph_state.model_copy(
        update={"agent_state": agent_state, "deterministic_issues": ()}
    )
    return _append_report_event(
        updated,
        event_type=ReportEventType.REVISION_APPLIED,
        summary="已执行唯一一次报告级安全收窄，等待第二轮独立审计。",
    )


async def _degrade_report(
    graph_state: ReportGraphState,
    runtime: Runtime[ReportGraphRuntime],
) -> ReportGraphState:
    """在无剩余返工预算时删除未放行结论并完成 degraded 终态。

    使用最后 AuditResult 的结构化 issues，不解析指令文本；降级稿不会再调用 Auditor，也不会生成
    memory candidate。状态和 outcome 原子写入，最后追加明确 SAFE_DEGRADED 事件。
    """

    report = graph_state.agent_state.draft_report
    audit_result = graph_state.agent_state.audit_result
    if report is None or audit_result is None or audit_result.status is not AuditStatus.REVISE:
        raise RuntimeError("degrade node requires a revise audit and draft report")
    degraded = runtime.context.reviser.degrade(
        report,
        tuple(audit_result.issues),
        graph_state.agent_state,
        evidence_bundle=graph_state.evidence_bundle,
        confirmed_case_memories=graph_state.confirmed_case_memories,
    )
    agent_state = graph_state.agent_state.model_copy(update={"draft_report": degraded})
    completed = graph_state.model_copy(
        update={
            "agent_state": agent_state,
            "status": ReportWorkflowStatus.COMPLETED,
            "outcome": ReportWorkflowOutcome.DEGRADED,
        }
    )
    return _append_report_event(
        completed,
        event_type=ReportEventType.SAFE_DEGRADED,
        summary="报告在唯一返工后仍未通过，已删除未放行结论并安全降级。",
        audit_status=AuditStatus.REVISE,
        issues=tuple(audit_result.issues),
    )


def _merge_audit_result(
    model_result: AuditResult,
    deterministic_issues: tuple[AuditIssue, ...],
) -> AuditResult:
    """合并模型和规则问题，并保证任何确定性问题都能否决 accept。

    去重键使用 code/claim_path/evidence_refs；模型 revision instruction 可保留为公开修订要求，但
    确定性问题只追加通用指令，不让模型覆盖。无任何规则问题时原样返回已校验模型结果。
    """

    if not deterministic_issues:
        return model_result
    combined_issues = _deduplicate_issues([*deterministic_issues, *model_result.issues])
    instructions = list(model_result.revision_instructions)
    instructions.append("先修复全部确定性引用、冲突和风险问题，再重新提交独立审计。")
    return AuditResult(
        status=AuditStatus.REVISE,
        issues=combined_issues,
        revision_instructions=list(dict.fromkeys(instructions)),
    )


def _deduplicate_issues(issues: list[AuditIssue]) -> list[AuditIssue]:
    """稳定去重合并后的 AuditIssue，保留确定性问题在前的优先顺序。

    message 不参与键，防止同一问题因 Auditor 措辞不同重复；首个对象保留，输出仍是强类型列表。
    空输入合法返回空列表，但 revise 构造会在 AuditResult 层拒绝它。
    """

    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    result: list[AuditIssue] = []
    for issue in issues:
        key = (issue.code.value, issue.claim_path, issue.evidence_refs)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _append_report_event(
    graph_state: ReportGraphState,
    *,
    event_type: ReportEventType,
    summary: str,
    audit_status: AuditStatus | None = None,
    issues: tuple[AuditIssue, ...] = (),
) -> ReportGraphState:
    """按单调序号生成稳定报告事件，并返回包含新列表的状态副本。

    ID 由 run_id、序号和类型计算，可重放且不含模型文本；问题只投影有限 code 并稳定去重。函数
    不修改已有 events，避免 LangGraph 快照共享可变列表。
    """

    sequence = len(graph_state.events) + 1
    event_id = _stable_event_id(
        graph_state.agent_state.run_id,
        str(sequence),
        event_type.value,
    )
    issue_codes = tuple(dict.fromkeys(issue.code for issue in issues))
    event = ReportPublicEvent(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        summary=summary,
        audit_status=audit_status,
        issue_codes=issue_codes,
        revision_number=graph_state.agent_state.retry_count,
    )
    return graph_state.model_copy(update={"events": [*graph_state.events, event]})


def _stable_event_id(*parts: str) -> str:
    """使用 SHA-256 生成可公开的 16 位稳定报告事件 ID。

    分隔符避免简单拼接歧义，前缀隔离 ReAct 与报告事件命名空间；截断只用于本作品审计引用，
    不承担认证或密码学唯一性。相同运行和控制流重放会得到相同 ID。
    """

    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"report_evt_{digest}"
