"""使用真实三段 LangGraph 工作流验证 Memory off/on 历史影响消融评测。

集成测试运行生产 ``BoundedReactLoop``、``AuditedReportWorkflow`` 和 ``AuditedDiagnosisWorkflow``；
Planner/Auditor 使用确定性协议替身，工具响应经过生产 Observation 标准化。测试不访问模型或生产
系统，只使用版本化合成案例，重点证明评测指标来自实际 Action/Observation 与最终审计报告。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from app.agents.auditor import AuditorTurnContext
from app.agents.planner import PlannerTurnContext
from app.capabilities import (
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
)
from app.domain.models import (
    AgentState,
    AuditResult,
    AuditStatus,
    CaseMemory,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
)
from app.domain.planner import PlannerDecision, PlannerStatus, ToolAction
from app.domain.tooling import (
    McpToolRequest,
    McpToolResponse,
    TimeRange,
    ToolEvidencePayload,
    ToolName,
)
from app.mcp.observation import ToolObservation, normalize_observation
from app.memory.models import (
    CaseMemoryMatch,
    MemoryRetrievalChannel,
    MemoryStageResult,
    MemoryStageStatus,
)
from app.orchestration import (
    AuditedDiagnosisWorkflow,
    AuditedReportWorkflow,
    BoundedReactLoop,
    DiagnosisRunRequest,
    DiagnosisRunResult,
    DiagnosisWorkflowConfig,
    HistoryImpactEvalCase,
    HistoryImpactMode,
    ReactLoopConfig,
    ReportWorkflowConfig,
    evaluate_history_impact,
    load_history_impact_eval_suite,
)
from app.orchestration.report_models import ReportRunResult

SUITE_PATH = Path("data/evals/history_impact_cases.json")
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


class HistoryAwareScriptedPlanner:
    """在真实 ReAct 子图中根据 confirmed 历史上下文选择合成只读 Action。

    action-guidance 案例在无历史时选择宽泛状态查询，有历史时选择 fixture 标注的依赖拓扑检查；
    其余案例两组选择相同必要工具。第二轮统一引用真实 Observation 并 finish，不输出 Thought。
    """

    def __init__(self, case: HistoryImpactEvalCase) -> None:
        """保存不可变评测案例并初始化 PlannerTurnContext 调用记录。

        构造不选择工具或读取历史；每次 ``decide`` 都从当前 context 判断 react_step 和历史候选，
        因而测试能证明顶层工作流确实把 memory-on 案例注入 Planner，而非 runner 旁路选择结果。
        """

        self._case = case
        self.contexts: list[PlannerTurnContext] = []

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """首轮返回一个结构化 ToolAction，次轮基于标准 Observation 安全结束。

        所有 evidence_refs 都来自当前强类型状态，工具参数使用本次 run_id 作为 trace；若循环调用
        超过两轮则抛出 AssertionError，避免错误边配置被更多脚本响应掩盖。
        """

        self.contexts.append(context)
        if context.state.react_step == 0:
            selected_tool = self._select_tool(context)
            action = ToolAction(
                tool_name=selected_tool,
                arguments=McpToolRequest(
                    resource_id=f"resource-{self._case.case_id}",
                    time_range=TimeRange(start=NOW - timedelta(minutes=5), end=NOW),
                    scenario_id=self._case.scenario_id,
                    trace_id=context.state.run_id,
                ),
            )
            return PlannerDecision(
                status=PlannerStatus.CALL_TOOL,
                decision_summary="选择一项只读工具补充当前 Observation。",
                action=action,
                evidence_refs=[context.state.evidence[0].evidence_id],
            )
        if context.state.react_step == 1:
            return PlannerDecision(
                status=PlannerStatus.FINISH,
                decision_summary="实时 Observation 已记录，可以进入独立报告审计。",
                evidence_refs=list(context.state.observation_refs),
                stop_reason="evidence_sufficient",
            )
        raise AssertionError("history impact Planner must finish after one tool Action")

    def _select_tool(self, context: PlannerTurnContext) -> ToolName:
        """根据当前案例和是否收到历史解释选择本轮实际只读工具。

        只有 action-guidance 案例允许历史改变选择：无历史使用宽泛状态查询，有历史使用必要拓扑
        查询。其他案例始终返回首个 required 工具，用于验证历史加入不会造成 Action 回归。
        """

        if (
            self._case.case_id == "history_impact_action_guidance"
            and not context.history_case_matches
        ):
            return ToolName.LTS_GET_TASK_STATUS
        return self._case.required_tool_names[0]


class AcceptingHistoryImpactAuditor:
    """记录真实 AuditorTurnContext，并接受已通过生产确定性门禁的合成报告。

    ``AuditedReportWorkflow`` 会先运行 Builder 与 ReportPolicyValidator；本替身只模拟独立模型的
    accept 结果，无法覆盖无效引用、未确认案例或历史解释漂移等客观问题。
    """

    def __init__(self) -> None:
        """初始化空上下文记录，不提前创建审计结果或修改报告。

        每个 off/on run 使用独立实例，防止历史上下文从实验组泄漏到对照组；构造不执行外部 I/O。
        构造无输入、无返回副作用，后续 review 的校验异常保持显式传播。
        """

        self.contexts: list[AuditorTurnContext] = []

    async def review(self, context: AuditorTurnContext) -> AuditResult:
        """保存完整审计上下文并返回字段严格的 accept 结果。

        如果确定性 Validator 已产生问题，报告工作流会合并问题并否决该 accept；因此最终 accepted
        同时证明根因引用、confirmed history 与 matcher 投影满足生产规则。
        """

        self.contexts.append(context)
        return AuditResult(status=AuditStatus.ACCEPT)


class SyntheticObservationExecutor:
    """把 Planner ToolAction 转成生产 ``ToolObservation`` 的合成只读执行器。

    执行器不读取 fixture 文件或访问网络；它构造统一 MCP 成功响应后调用 ``normalize_observation``，
    因而 Evidence/ToolEvent ID、trace、请求和时间语义与生产 MCP 客户端保持同一边界。
    """

    def __init__(self, case: HistoryImpactEvalCase) -> None:
        """保存当前评测案例并初始化实际 Action 记录。

        构造不执行工具；记录列表用于集成断言每个 run 只有一次 Action，避免重试或重复调用改变
        意外 Action 指标。案例只包含合成 scenario 和公开工具名。
        """

        self._case = case
        self.actions: list[ToolAction] = []

    async def execute(self, action: ToolAction) -> ToolObservation:
        """记录 Action，生成一条合成证据并通过生产标准化函数返回 Observation。

        响应内容只说明执行了只读检查，不增加新的根因；当前根因仍由运行前 TOOL Evidence 支持。
        started/completed 使用带时区且递增时间，任何 Schema 错误都会显式传播。
        """

        self.actions.append(action)
        response = McpToolResponse(
            ok=True,
            data={"status": "synthetic", "scenario_id": self._case.scenario_id},
            evidence=[
                ToolEvidencePayload(
                    source_id=f"source-{self._case.case_id}-{action.tool_name.value}",
                    content=f"合成只读工具 {action.tool_name.value} 已返回本次 Observation。",
                    metadata={"scenario_id": self._case.scenario_id},
                )
            ],
            observed_at=NOW,
        )
        return normalize_observation(
            action=action,
            response=response,
            started_at=NOW,
            completed_at=NOW + timedelta(milliseconds=20),
            attempt=1,
        )


class ScriptedHistoryMemoryWorkflow:
    """为 memory-on 返回一个 confirmed 案例，并为 accepted 报告生成 pending 候选。

    顶层图决定是否调用 ``search``；memory-off 若错误访问搜索会被调用记录暴露。该替身不计算
    embedding 或 SQL，检索真实性已由独立 PostgreSQL/pgvector 消融切片覆盖。
    """

    def __init__(self, case: HistoryImpactEvalCase) -> None:
        """保存案例、创建唯一 confirmed match 并初始化搜索/staging 记录。

        冲突案例的历史根因取 forbidden 标注，其余取 allowed 根因；所有案例保持 confirmed，避免
        把状态隔离问题混入本次端到端影响评测。
        """

        self._case = case
        self._match = _confirmed_history_match(case)
        self.search_calls: list[tuple[str, int | None]] = []
        self.stage_calls: list[ReportRunResult] = []

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[CaseMemoryMatch]:
        """记录预算化查询并返回不超过 limit 的单个 confirmed 合成命中。

        空 query 或非正 limit 会由上游配置/模型阻止；本替身保留实际 query 供断言，不把失败转换为
        空命中。返回新列表，调用方不能修改内部 match 快照。
        """

        self.search_calls.append((query, limit))
        matches = [self._match]
        return matches if limit is None else matches[:limit]

    async def stage(self, result: ReportRunResult) -> MemoryStageResult:
        """记录已审计报告并返回只含本次证据的新 pending 案例。

        方法不自动 confirm，也不复制旧历史 Evidence；accepted 资格仍由生产顶层调用顺序和报告
        结果保证。若报告没有根因，索引失败会暴露脚本/Builder 回归而不是伪造候选。
        """

        self.stage_calls.append(result)
        report = result.state.draft_report
        if report is None or not report.root_causes:
            raise AssertionError("history impact staging requires an accepted root cause")
        memory = CaseMemory(
            memory_id=f"mem_{_digest(result.state.run_id, 'pending')}",
            symptoms=[self._case.user_query],
            root_cause=report.root_causes[0].root_cause,
            components=list(self._case.components),
            evidence_refs=list(report.root_causes[0].evidence_refs),
            status=MemoryStatus.PENDING,
            created_at=NOW,
            updated_at=NOW,
        )
        return MemoryStageResult(status=MemoryStageStatus.STAGED, memory=memory)


class LangGraphHistoryImpactRunner:
    """按评测模式构造并运行真实三段 LangGraph 的可重复 runner。

    每次 run 创建独立 Planner、Auditor、Executor 和 Memory workflow，消除 paired 实验间可变状态；
    只把 capability history trigger 从 not_requested 切换为 user_requested，其余 case 输入保持一致。
    """

    def __init__(self) -> None:
        """初始化结果与各边界调用记录，等待 evaluator 顺序驱动六次运行。

        列表只保存公开强类型对象和计数，不记录 Thought、Prompt 原文或凭据；构造不编译图或执行 I/O。
        """

        self.results: list[DiagnosisRunResult] = []
        self.planner_contexts: list[list[PlannerTurnContext]] = []
        self.auditor_contexts: list[list[AuditorTurnContext]] = []
        self.tool_actions: list[list[ToolAction]] = []
        self.search_counts: list[int] = []

    async def run(
        self,
        case: HistoryImpactEvalCase,
        *,
        mode: HistoryImpactMode,
    ) -> DiagnosisRunResult:
        """运行一个隔离的真实 LangGraph 诊断，并保存可供集成断言的边界记录。

        初始状态、capability、预算和 evaluator case 相同；mode 只决定历史 trigger。任一 Planner、
        Observation、Auditor 或 staging 异常原样传播，不能返回部分结果。
        """

        planner = HistoryAwareScriptedPlanner(case)
        auditor = AcceptingHistoryImpactAuditor()
        executor = SyntheticObservationExecutor(case)
        memory = ScriptedHistoryMemoryWorkflow(case)
        workflow = AuditedDiagnosisWorkflow(
            react=BoundedReactLoop(
                planner=planner,
                executor=executor,
                config=ReactLoopConfig(max_steps=3, total_timeout_seconds=5),
            ),
            report=AuditedReportWorkflow(
                auditor=auditor,
                config=ReportWorkflowConfig(max_revisions=1),
            ),
            memory=memory,
            config=DiagnosisWorkflowConfig(memory_search_limit=3, memory_query_max_chars=1000),
        )
        result = await workflow.run(
            DiagnosisRunRequest(
                state=_initial_state(case, mode=mode),
                capability_request=CapabilitySelectionRequest(
                    intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
                    components=tuple(case.components),
                    history_trigger=(
                        HistoryTrigger.USER_REQUESTED
                        if mode is HistoryImpactMode.MEMORY_ON
                        else HistoryTrigger.NOT_REQUESTED
                    ),
                ),
            )
        )
        self.results.append(result)
        self.planner_contexts.append(planner.contexts)
        self.auditor_contexts.append(auditor.contexts)
        self.tool_actions.append(executor.actions)
        self.search_counts.append(len(memory.search_calls))
        return result


@pytest.mark.asyncio
async def test_real_langgraph_history_impact_ablation_matches_measured_contract() -> None:
    """验证三条合成案例通过真实 LangGraph 后得到固定 Memory off/on 实测结果。

    断言覆盖 6 次运行、每次一项实际 ToolAction、off 不搜索/on 搜索一次、Planner/Auditor 历史上下文
    对齐，以及宏观 Action 增益、根因/引用稳定、历史投影和冲突保护全部通过。
    """

    suite = load_history_impact_eval_suite(SUITE_PATH)
    runner = LangGraphHistoryImpactRunner()

    report = await evaluate_history_impact(suite, runner)

    assert len(runner.results) == 6
    assert all(len(actions) == 1 for actions in runner.tool_actions)
    assert runner.search_counts == [0, 1, 0, 1, 0, 1]
    assert all(not contexts[0].history_case_matches for contexts in runner.planner_contexts[::2])
    assert all(contexts[0].history_case_matches for contexts in runner.planner_contexts[1::2])
    assert all(not contexts[0].history_case_matches for contexts in runner.auditor_contexts[::2])
    assert all(contexts[0].history_case_matches for contexts in runner.auditor_contexts[1::2])
    assert report.memory_off_macro_action_coverage == pytest.approx(2 / 3)
    assert report.memory_on_macro_action_coverage == 1
    assert report.action_coverage_delta == pytest.approx(1 / 3)
    assert report.memory_off_macro_unexpected_action_rate == pytest.approx(1 / 3)
    assert report.memory_on_macro_unexpected_action_rate == 0
    assert report.memory_off_root_cause_hit_rate == 1
    assert report.memory_on_root_cause_hit_rate == 1
    assert report.memory_off_realtime_citation_rate == 1
    assert report.memory_on_realtime_citation_rate == 1
    assert report.history_projection_pass_rate == 1
    assert report.conflict_guard_pass_rate == 1
    assert report.action_regression_count == 0
    assert report.realtime_priority_failure_count == 0


def _initial_state(
    case: HistoryImpactEvalCase,
    *,
    mode: HistoryImpactMode,
) -> AgentState:
    """构造两组共享问题/根因语义、但 run/session ID 隔离的初始诊断状态。

    预置 TOOL Evidence 代表本次实时事实并支持 allowed 根因；历史案例尚未进入状态。Planner 后续
    Action 只补充 Observation，不改变根因，使评测能客观检查旧案例是否覆盖当前事实。
    """

    current_root = case.allowed_root_causes[0]
    run_id = f"run_{_digest(case.case_id, mode.value)}"
    evidence_id = f"ev_{_digest(case.case_id, mode.value, 'current')}"
    return AgentState(
        run_id=run_id,
        session_id=f"session_{_digest(case.case_id, mode.value, 'session')}",
        user_query=case.user_query,
        hypotheses=[
            FaultHypothesis(
                hypothesis_id=f"hyp_{_digest(case.case_id, mode.value)}",
                symptom=f"{case.components[0].value} 合成故障",
                candidate_root_cause=current_root,
                components=list(case.components),
                supporting_evidence=[evidence_id],
                status=HypothesisStatus.SUPPORTED,
                confidence=0.9,
            )
        ],
        evidence=[
            Evidence(
                evidence_id=evidence_id,
                source_type=EvidenceSourceType.TOOL,
                source_id=f"synthetic-current-{case.scenario_id}",
                content=f"本次实时只读 Observation 支持当前根因：{current_root}。",
                observed_at=NOW,
                reliability=0.97,
            )
        ],
    )


def _confirmed_history_match(case: HistoryImpactEvalCase) -> CaseMemoryMatch:
    """根据案例标注创建一个 vector 通道的 confirmed 历史命中。

    冲突案例使用 forbidden 根因，用于验证 matcher 生成差异和禁止复用提示；其他案例使用 allowed
    根因。历史方案只包含隔离环境人工复核，不表示生产修复已经执行。
    """

    root_cause = (
        case.forbidden_root_causes[0]
        if case.expect_history_conflict
        else case.allowed_root_causes[0]
    )
    memory = CaseMemory(
        memory_id=f"mem_{_digest(case.case_id, 'confirmed-history')}",
        symptoms=[case.user_query],
        root_cause=root_cause,
        fault_path=["合成历史故障路径"],
        solution_steps=["仅在隔离环境人工复核历史方案。"],
        components=list(case.components),
        tags=["history_impact_eval"],
        evidence_refs=[f"ev_{_digest(case.case_id, 'historical-evidence')}"],
        status=MemoryStatus.CONFIRMED,
        occurrence_count=2,
        created_at=NOW - timedelta(days=3),
        updated_at=NOW - timedelta(days=1),
    )
    return CaseMemoryMatch(
        memory=memory,
        similarity=0.91,
        retrieval_channels=[MemoryRetrievalChannel.VECTOR],
        direct_similarity=0.91,
    )


def _digest(*parts: str) -> str:
    """生成稳定 16 位十六进制合成 ID 片段，支持 paired run 可重复重放。

    SHA-256 截断只服务测试可寻址性，不用于凭据或安全令牌；分隔符阻止部件简单拼接产生歧义。
    """

    return sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
