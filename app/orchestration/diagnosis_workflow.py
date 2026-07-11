"""用顶层 LangGraph 串联按需历史召回、Planner ReAct、Auditor 报告和记忆暂存。

本模块只做确定性编排：两个 LLM Agent 仍分别位于既有 ReAct/报告子图，案例查询与写入仍由记忆
runtime 执行。任何数据库或子图异常都会传播，不能静默伪装为空历史或成功报告。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.capabilities import HistoryTrigger
from app.domain.models import AgentState, EvidenceSourceType
from app.memory.models import CaseMemoryMatch, MemoryStageResult
from app.orchestration.diagnosis_models import (
    DIAGNOSIS_WORKFLOW_CONTRACT_ID,
    DiagnosisGraphState,
    DiagnosisRunRequest,
    DiagnosisRunResult,
    DiagnosisWorkflowConfig,
    DiagnosisWorkflowStatus,
)
from app.orchestration.models import ReactRunRequest, ReactRunResult
from app.orchestration.report_models import ReportRunRequest, ReportRunResult


class ReactWorkflow(Protocol):
    """声明顶层编排调用有界 Planner ReAct 子图所需的最小异步接口。

    生产 ``BoundedReactLoop`` 与测试替身都可满足；接口只接收/返回强类型契约，不暴露 LangGraph
    内部状态或 Planner Provider，使顶层工作流不能绕过 Action/Observation 门禁。
    """

    async def run(self, request: ReactRunRequest) -> ReactRunResult:
        """执行 Planner ReAct 子图并返回带公开终止事件的结果。

        输入中的 confirmed 案例已经由记忆仓储过滤；实现异常必须传播，不能返回未停止状态或
        松散字典。工具预算、总超时和 MCP 失败语义仍由具体子图负责。
        """

        ...


class ReportWorkflow(Protocol):
    """声明顶层编排调用独立 Auditor 报告子图所需的最小异步接口。

    协议让生产 ``AuditedReportWorkflow`` 与测试替身共享边界；报告实现只能使用 ReAct 终态、
    GraphRAG Bundle 和同一批 confirmed 案例，不能自行重新查询记忆或执行工具。
    """

    async def run(self, request: ReportRunRequest) -> ReportRunResult:
        """执行确定性草稿、Auditor 和最多一次返工并返回 accepted/degraded 结果。

        请求必须来自已停止 ReAct；实现需保留公开审计事件。Provider/Schema 失败按报告子图规则
        安全降级，而编程错误继续抛出供顶层调用方处理。
        """

        ...


class CaseMemoryWorkflow(Protocol):
    """声明顶层编排需要的 confirmed 搜索和审计后暂存接口。

    生产 ``PostgresMemoryRuntime`` 为每次调用创建独立会话/事务；协议不暴露 confirm/reject，避免
    自动诊断擅自代替用户改变确认状态。搜索和 staging 错误必须显式传播。
    """

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[CaseMemoryMatch]:
        """按查询返回 confirmed 案例及相似度，未命中返回空列表。

        ``limit`` 是顶层上下文预算；实现必须在存储层排除 pending/rejected，并在 Provider 或数据库
        失败时抛出异常，不能把依赖故障解释为真实的零命中。
        """

        ...

    async def stage(self, result: ReportRunResult) -> MemoryStageResult:
        """把报告终态暂存、合并或按审计/根因门禁安全跳过。

        方法必须让 accepted 资格、事务、去重和 same-run 幂等保持在记忆服务内；顶层工作流只负责
        调用顺序，不复制根因投影规则或直接操作 ORM。
        """

        ...


@dataclass(frozen=True, slots=True)
class DiagnosisGraphRuntime:
    """保存顶层图共享但不进入序列化状态的三个子工作流和集中配置。

    依赖在构造后只读，单次运行的 query、matches 和结果都位于 ``DiagnosisGraphState``，因此并发
    会话不会共享可变诊断数据。具体 Provider/数据库资源仍由外层 lifespan 管理。
    """

    react: ReactWorkflow
    report: ReportWorkflow
    memory: CaseMemoryWorkflow
    config: DiagnosisWorkflowConfig


class AuditedDiagnosisWorkflow:
    """编译并运行 recall → ReAct → report/audit → stage memory 的固定顶层图。

    history trigger 为 not_requested 时确定性跳过搜索；其他触发才构造预算化查询。相同 recalled
    memories 同时进入 Planner 与 Auditor，最终无论 accepted/degraded 都调用 staging，由记忆服务
    返回明确写入或跳过状态。
    """

    def __init__(
        self,
        *,
        react: ReactWorkflow,
        report: ReportWorkflow,
        memory: CaseMemoryWorkflow,
        config: DiagnosisWorkflowConfig,
    ) -> None:
        """注入三段可替换工作流和预算配置，并一次编译固定 LangGraph。

        构造不查询数据库、不调用模型或 MCP；协议依赖便于单测记录跨阶段输入。图拓扑在构造时
        固定，运行节点不能增加 Agent、跳过审计或把 staging 提前到报告放行之前。
        """

        self._runtime = DiagnosisGraphRuntime(
            react=react,
            report=report,
            memory=memory,
            config=config,
        )
        self._graph = _build_diagnosis_graph()

    async def run(self, request: DiagnosisRunRequest) -> DiagnosisRunResult:
        """执行完整顶层图并返回可由 API/评测直接消费的强类型结果。

        请求先经过 Pydantic 校验；图依次保存每个已完成阶段。任何记忆、ReAct 或报告异常原样传播，
        避免返回缺阶段的伪成功。终态还会校验 history trigger、run/session 和
        staging/outcome 一致性。
        """

        initial = DiagnosisGraphState(
            initial_state=request.state,
            capability_request=request.capability_request,
            evidence_bundle=request.evidence_bundle,
        )
        raw_state = await self._graph.ainvoke(
            initial,
            context=self._runtime,
            config={"recursion_limit": 8},
        )
        final_state = DiagnosisGraphState.model_validate(raw_state)
        if final_state.status is not DiagnosisWorkflowStatus.COMPLETED:
            raise RuntimeError("diagnosis graph ended without completed status")
        if (
            final_state.react_result is None
            or final_state.report_result is None
            or final_state.memory_stage is None
        ):
            raise RuntimeError("completed diagnosis graph is missing a required stage result")
        return DiagnosisRunResult(
            contract_id=DIAGNOSIS_WORKFLOW_CONTRACT_ID,
            history_trigger=request.capability_request.history_trigger,
            memory_query=final_state.memory_query,
            recalled_memories=final_state.recalled_memories,
            react=final_state.react_result,
            report=final_state.report_result,
            memory_stage=final_state.memory_stage,
        )


def _build_diagnosis_graph():
    """构建四节点固定拓扑并编译为可复用 LangGraph。

    每个节点只有一条后继边，因为 accepted/degraded 分支已在报告子图内部收敛；顶层始终执行
    ``stage_case_memory``，由记忆服务根据审计 outcome 产生写入或 skipped 结果。
    """

    graph = StateGraph(DiagnosisGraphState, context_schema=DiagnosisGraphRuntime)
    graph.add_node("recall_case_memories", _recall_case_memories)
    graph.add_node("run_react", _run_react)
    graph.add_node("run_report", _run_report)
    graph.add_node("stage_case_memory", _stage_case_memory)
    graph.add_edge(START, "recall_case_memories")
    graph.add_edge("recall_case_memories", "run_react")
    graph.add_edge("run_react", "run_report")
    graph.add_edge("run_report", "stage_case_memory")
    graph.add_edge("stage_case_memory", END)
    return graph.compile(name="dataops_audited_diagnosis_v1")


async def _recall_case_memories(
    graph_state: DiagnosisGraphState,
    runtime: Runtime[DiagnosisGraphRuntime],
) -> DiagnosisGraphState:
    """按 capability history trigger 决定跳过或执行 confirmed 案例搜索。

    not_requested 不调用数据库，避免每轮诊断固定支付向量查询；显式触发时查询文本优先放用户问题
    和实时 Observation，再补假设，并受字符预算截断。搜索异常传播，不能伪装为零命中。
    """

    if graph_state.capability_request.history_trigger is HistoryTrigger.NOT_REQUESTED:
        return graph_state.model_copy(update={"memory_query": None, "recalled_memories": ()})

    query = _build_memory_query(
        graph_state.initial_state,
        max_chars=runtime.context.config.memory_query_max_chars,
    )
    matches = await runtime.context.memory.search(
        query,
        limit=runtime.context.config.memory_search_limit,
    )
    return graph_state.model_copy(
        update={
            "memory_query": query,
            "recalled_memories": tuple(matches),
        }
    )


async def _run_react(
    graph_state: DiagnosisGraphState,
    runtime: Runtime[DiagnosisGraphRuntime],
) -> DiagnosisGraphState:
    """把召回的 confirmed 案例注入有界 Planner ReAct 子图并保存终态。

    只投影 ``CaseMemoryMatch.memory``，similarity 仍保留在顶层结果供 API/评测展示；状态模型会再次
    拒绝非 confirmed 案例。ReAct 负责 capability 选择、MCP 和停止语义，顶层不解析 Planner 输出。
    """

    memories = tuple(match.memory for match in graph_state.recalled_memories)
    result = await runtime.context.react.run(
        ReactRunRequest(
            state=graph_state.initial_state,
            capability_request=graph_state.capability_request,
            evidence_bundle=graph_state.evidence_bundle,
            confirmed_case_memories=memories,
        )
    )
    return graph_state.model_copy(update={"react_result": result})


async def _run_report(
    graph_state: DiagnosisGraphState,
    runtime: Runtime[DiagnosisGraphRuntime],
) -> DiagnosisGraphState:
    """用 ReAct 终态和同一批 confirmed 案例运行报告/Auditor 子图。

    同一历史上下文同时供 Builder、确定性规则和 Auditor 使用，避免 Planner 看过的案例在审计阶段
    消失。若 ReAct 结果缺失则视为图编程错误；报告的 accepted/degraded 语义不在此改写。
    """

    if graph_state.react_result is None:
        raise RuntimeError("report node requires completed React result")
    # 复用 recall 节点的同一不可变快照，避免 ReAct 与 Auditor 之间再次查询导致确认状态或排序漂移。
    memories = tuple(match.memory for match in graph_state.recalled_memories)
    result = await runtime.context.report.run(
        ReportRunRequest(
            state=graph_state.react_result.state,
            capabilities=graph_state.react_result.capabilities,
            evidence_bundle=graph_state.evidence_bundle,
            confirmed_case_memories=memories,
        )
    )
    return graph_state.model_copy(update={"report_result": result})


async def _stage_case_memory(
    graph_state: DiagnosisGraphState,
    runtime: Runtime[DiagnosisGraphRuntime],
) -> DiagnosisGraphState:
    """在 Auditor 子图结束后调用长期记忆门禁并把顶层状态标记 completed。

    节点对 accepted 与 degraded 使用同一调用路径，避免顶层复制审计资格判断；记忆服务分别返回
    staged/merged、skipped_no_root_cause 或 skipped_not_accepted。异常会阻止 completed，保证 API
    不会在持久化未知时宣称诊断收尾成功。
    """

    if graph_state.report_result is None:
        raise RuntimeError("memory stage node requires completed report result")
    stage = await runtime.context.memory.stage(graph_state.report_result)
    # staging 是当前 run 的最终确定性投影；重新验证完整 ReportRunResult，可清除恢复状态中的旧候选，
    # 也让后续 checkpoint/API 从 AgentState 与外层 memory_stage 读取到完全一致的对象。
    report_state = graph_state.report_result.state.model_copy(
        update={"memory_candidate": stage.memory}
    )
    report_result = ReportRunResult.model_validate(
        {
            **graph_state.report_result.model_dump(),
            "state": report_state,
        }
    )
    return graph_state.model_copy(
        update={
            "report_result": report_result,
            "memory_stage": stage,
            "status": DiagnosisWorkflowStatus.COMPLETED,
        }
    )


def _build_memory_query(state: AgentState, *, max_chars: int) -> str:
    """把用户问题、实时 Evidence 和当前假设按优先级组合成预算化记忆查询。

    CASE_MEMORY 来源不会再次进入查询，避免历史案例递归强化自身；实时 Evidence 排在假设前，使
    截断时保留本次 Observation。结果按字符边界截断并去除尾部空白，纯空输入显式失败。
    """

    segments = [f"用户问题: {state.user_query.strip()}"]
    for evidence in state.evidence:
        if evidence.source_type is EvidenceSourceType.CASE_MEMORY:
            continue
        segments.append(f"实时观察: {evidence.content.strip()}")
    for hypothesis in state.hypotheses:
        components = ",".join(component.value for component in hypothesis.components)
        segments.append(
            "当前假设: "
            f"症状={hypothesis.symptom.strip()}; "
            f"根因={hypothesis.candidate_root_cause.strip()}; "
            f"组件={components}"
        )

    # 统一在组合后截断，保证同一状态稳定生成同一查询，同时让高优先级实时事实位于预算前部。
    query = "\n".join(segment for segment in segments if segment.strip())[:max_chars].rstrip()
    if not query:
        raise ValueError("memory recall query must not be blank")
    return query
