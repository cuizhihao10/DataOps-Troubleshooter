"""贯通顶层诊断图、两个真实 LangGraph 子图和 PostgreSQL 长期记忆的跨会话闭环。

测试使用结构化 Planner/Auditor 替身与确定性 Embedding，不访问付费模型；初始 Evidence 为合成
实时观察。第一次诊断暂存并确认案例，第二个会话按需召回同一案例并在审计后幂等合并。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

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
    Component,
    Evidence,
    EvidenceSourceType,
    FaultHypothesis,
    HypothesisStatus,
    MemoryStatus,
)
from app.domain.planner import PlannerDecision, PlannerStatus, ToolAction
from app.mcp.observation import ToolObservation
from app.memory.models import MemoryDecision, MemoryStageStatus
from app.memory.runtime import PostgresMemoryRuntime
from app.orchestration import (
    AuditedDiagnosisWorkflow,
    AuditedReportWorkflow,
    BoundedReactLoop,
    DiagnosisRunRequest,
    DiagnosisWorkflowConfig,
    ReactLoopConfig,
    ReportWorkflowConfig,
)
from app.persistence.database import create_database_engine, create_session_factory
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider

DATABASE_URL = os.getenv("DATAOPS_TEST_DATABASE_URL")
NOW = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


class FinishFromCurrentEvidencePlanner:
    """每次根据当前状态第一条 Evidence 返回合法 finish，并记录 Planner 上下文。

    替身不生成 Action，因此 MCP 执行器不应被调用；第二次上下文用于证明 confirmed 历史案例已经
    由顶层 workflow 注入真实 PlannerTurnContext，而不是只保存在最终结果旁路字段。
    """

    def __init__(self) -> None:
        """初始化空上下文记录，不预置固定 evidence_id 或运行次数。

        decide 从实际状态读取引用，因此同一 Planner 实例可安全处理两个不同 run；构造不调用模型、
        数据库或 MCP，也不会保存跨测试全局状态。
        """

        self.contexts: list[PlannerTurnContext] = []

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """记录上下文并用当前实时 Evidence 返回结构化 finish 决策。

        输入必须至少有一条 Evidence，否则索引失败显式暴露不完整诊断状态。输出不包含 Thought、
        Action 或新事实，只说明现有证据已经足够进入报告阶段。
        """

        self.contexts.append(context)
        evidence_id = context.state.evidence[0].evidence_id
        return PlannerDecision(
            status=PlannerStatus.FINISH,
            decision_summary="现有合成实时证据足以进入独立审计。",
            evidence_refs=[evidence_id],
            stop_reason="evidence_sufficient",
        )


class AcceptingAuditor:
    """记录真实 AuditorTurnContext，并对通过确定性规则的合成报告返回 accept。

    报告工作流仍会先执行生产 Builder 与 ReportPolicyValidator，因此本替身不能越过客观引用门禁；
    第二次上下文用于断言 recalled confirmed 案例也进入独立 Auditor。
    """

    def __init__(self) -> None:
        """初始化空审计上下文列表，不预先生成 AuditResult 或报告。

        每次 review 返回新的冻结 Pydantic 对象；构造不调用模型，也不修改报告状态，适合跨两个
        会话复用并精确检查调用次数。
        """

        self.contexts: list[AuditorTurnContext] = []

    async def review(self, context: AuditorTurnContext) -> AuditResult:
        """保存已包含确定性问题的上下文，并返回无问题的结构化 accept。

        若生产 Validator 发现问题，报告工作流仍拥有最终否决权并会把 accept 合并为 revise；因此
        测试通过最终 accepted 结果同时证明草稿引用和历史状态均满足客观规则。
        """

        self.contexts.append(context)
        return AuditResult(status=AuditStatus.ACCEPT)


class FailIfToolCalledExecutor:
    """在收到任何 ToolAction 时立即失败，证明本场景只使用已有实时 Evidence。

    顶层工作流仍运行真实 BoundedReactLoop；该替身把意外 Action 变成明确 AssertionError，避免
    测试因执行额外 Mock 工具仍然成功而掩盖 Planner 脚本或循环边配置漂移。
    """

    async def execute(self, action: ToolAction) -> ToolObservation:
        """拒绝所有 Action，并在错误消息中保留工具名供定位。

        本测试的 Planner 第一轮必定 finish，因此正常路径永不调用该方法；若控制流改变，异常原样
        传播，不构造伪 Observation 或 ToolEvent。
        """

        raise AssertionError(f"unexpected tool call: {action.tool_name.value}")


def _state(*, run_id: str, session_id: str, evidence_id: str, observed_at: datetime) -> AgentState:
    """构造带 supported 假设和真实来源类型 Evidence 的后续可审计诊断初态。

    两个会话只改变 run/session/evidence ID 和时间，症状/根因保持一致以触发 exact signature 合并；
    状态不预填 intent、capability、stop 或报告，确保真实子图完成这些转换。
    """

    evidence = Evidence(
        evidence_id=evidence_id,
        source_type=EvidenceSourceType.TOOL,
        source_id=f"source_{run_id}",
        content="实时只读工具确认 LTS 任务等待上游数据。",
        observed_at=observed_at,
        reliability=0.96,
    )
    return AgentState(
        run_id=run_id,
        session_id=session_id,
        user_query="检查 LTS 任务等待上游数据并参考相似历史案例",
        hypotheses=[
            FaultHypothesis(
                hypothesis_id=f"hyp_{run_id}",
                symptom="LTS 任务等待上游",
                candidate_root_cause="上游数据未按时就绪",
                components=[Component.LTS],
                supporting_evidence=[evidence_id],
                status=HypothesisStatus.SUPPORTED,
                confidence=0.9,
            )
        ],
        evidence=[evidence],
    )


def _capability_request(history_trigger: HistoryTrigger) -> CapabilitySelectionRequest:
    """创建 LTS 单组件 capability 请求，并显式控制是否启用历史匹配。

    路由不解析自然语言；第一次 not_requested 证明不会搜索，第二次 user_requested 证明 PostgreSQL
    confirmed 召回发生。组件数量满足生产 registry 的单组件约束。
    """

    return CapabilitySelectionRequest(
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
        history_trigger=history_trigger,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_confirmed_memory_is_recalled_next_session_and_new_report_is_merged() -> None:
    """验证首次 accepted 诊断暂存/确认后，下一会话真实召回并再次合并同一案例。

    断言覆盖：首轮不查询但生成 pending；用户确认后第二轮 pgvector 搜索命中；confirmed 案例同时
    进入 Planner/Auditor；最终 exact signature 合并、occurrence=2 且 Evidence 保留两个 run 来源。
    """

    if DATABASE_URL is None:
        pytest.fail("DATAOPS_TEST_DATABASE_URL is required for postgres tests")
    engine = create_database_engine(DATABASE_URL)
    factory = create_session_factory(engine)
    memory = PostgresMemoryRuntime(
        factory,
        DeterministicHashEmbeddingProvider(dimensions=128),
        dedup_similarity_threshold=0.92,
        default_search_limit=5,
    )
    planner = FinishFromCurrentEvidencePlanner()
    auditor = AcceptingAuditor()
    workflow = AuditedDiagnosisWorkflow(
        react=BoundedReactLoop(
            planner=planner,
            executor=FailIfToolCalledExecutor(),
            config=ReactLoopConfig(max_steps=6, total_timeout_seconds=5),
        ),
        report=AuditedReportWorkflow(
            auditor=auditor,
            config=ReportWorkflowConfig(max_revisions=1),
        ),
        memory=memory,
        config=DiagnosisWorkflowConfig(memory_search_limit=3, memory_query_max_chars=1000),
    )

    try:
        # 案例表由本专项测试独占；知识图种子不修改，关联表必须先删以满足外键顺序。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))

        first = await workflow.run(
            DiagnosisRunRequest(
                state=_state(
                    run_id="run_diagnosis_pg_001",
                    session_id="session_diagnosis_pg_001",
                    evidence_id="ev_diagnosis_pg_001",
                    observed_at=NOW,
                ),
                capability_request=_capability_request(HistoryTrigger.NOT_REQUESTED),
            )
        )
        assert first.recalled_memories == ()
        assert first.memory_stage.status is MemoryStageStatus.STAGED
        assert first.memory_stage.memory is not None
        assert first.report.state.memory_candidate == first.memory_stage.memory
        memory_id = first.memory_stage.memory.memory_id
        confirmed = await memory.decide(memory_id, MemoryDecision.CONFIRM)
        assert confirmed is not None and confirmed.status is MemoryStatus.CONFIRMED

        second = await workflow.run(
            DiagnosisRunRequest(
                state=_state(
                    run_id="run_diagnosis_pg_002",
                    session_id="session_diagnosis_pg_002",
                    evidence_id="ev_diagnosis_pg_002",
                    observed_at=NOW + timedelta(minutes=1),
                ),
                capability_request=_capability_request(HistoryTrigger.USER_REQUESTED),
            )
        )

        assert second.memory_query is not None
        assert "实时只读工具确认" in second.memory_query
        assert len(second.recalled_memories) == 1
        assert second.recalled_memories[0].memory.memory_id == memory_id
        assert planner.contexts[0].confirmed_case_memories == ()
        assert planner.contexts[1].confirmed_case_memories[0].memory_id == memory_id
        assert auditor.contexts[0].confirmed_case_memories == ()
        assert auditor.contexts[1].confirmed_case_memories[0].memory_id == memory_id
        assert second.memory_stage.status is MemoryStageStatus.MERGED
        assert second.memory_stage.memory is not None
        assert second.memory_stage.memory.status is MemoryStatus.CONFIRMED
        assert second.memory_stage.memory.occurrence_count == 2
        assert second.report.state.memory_candidate == second.memory_stage.memory
        assert second.memory_stage.memory.evidence_refs == [
            "ev_diagnosis_pg_001",
            "ev_diagnosis_pg_002",
        ]
        visible = await memory.search("LTS 上游数据未就绪")
        assert len(visible) == 1
        assert visible[0].memory.occurrence_count == 2
    finally:
        # 失败路径同样清理合成案例并释放 asyncpg 池，避免后续 postgres marker 观察到残留状态。
        async with factory.begin() as session:
            await session.execute(text("DELETE FROM memory_evidence"))
            await session.execute(text("DELETE FROM case_memories"))
        await engine.dispose()
