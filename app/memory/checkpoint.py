"""定义短期会话 checkpoint 的安全快照、构建和恢复规则。

checkpoint 与跨会话 ``case_memories`` 完全分离：它只服务一个 session 的追问，保存公开
AgentState 子集和上一轮报告，并在新 run 开始时重建新的瞬态状态。模块不执行数据库 I/O，便于
用纯单元测试验证不会恢复 Thought、旧 run 身份或已经耗尽的 ReAct 预算。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import (
    AgentState,
    DiagnosisReport,
    Evidence,
    FaultHypothesis,
    RetrievedPath,
    SessionTurnContext,
    ToolEvent,
)
from app.orchestration.diagnosis_models import DiagnosisRunResult
from app.orchestration.report_models import ReportWorkflowOutcome

SESSION_CHECKPOINT_CONTRACT_ID = "session-checkpoint:v1"

# 检查点是“滚动窗口”而不是无限 transcript。保留最近公开状态可以让追问继续使用
# 最新证据，同时给 JSONB、检索查询和恢复 Pydantic 校验设定确定上限；真正的长期
# 案例仍由 case_memories 单独保存，因此这里不需要复制整个历史会话。
CHECKPOINT_MAX_PLAN_ITEMS = 16
CHECKPOINT_MAX_HYPOTHESES = 16
CHECKPOINT_MAX_EVIDENCE = 64
CHECKPOINT_MAX_TOOL_EVENTS = 64
CHECKPOINT_MAX_PATHS = 32
CHECKPOINT_MAX_OBSERVATION_REFS = 128


class SessionCheckpoint(BaseModel):
    """表示一个 session 最新成功 run 的版本化、可序列化短期状态快照。

    ``checkpoint_version`` 是会话内单调轮次，不是 Schema 版本；Schema 版本由 ``contract_id``
    固定。快照只保存下一轮 Planner 所需的计划、假设、证据、工具事件、图路径和最终报告，明确
    排除 next_action、stop_reason、audit_result、memory_candidate 与旧 react_step。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["session-checkpoint:v1"] = SESSION_CHECKPOINT_CONTRACT_ID
    checkpoint_version: int = Field(ge=1)
    session_id: str = Field(min_length=1, max_length=100)
    source_run_id: str = Field(min_length=1, max_length=100)
    source_user_query: str = Field(min_length=1, max_length=4000)
    plan: tuple[str, ...] = ()
    hypotheses: tuple[FaultHypothesis, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    tool_events: tuple[ToolEvent, ...] = ()
    retrieved_paths: tuple[RetrievedPath, ...] = ()
    observation_refs: tuple[str, ...] = ()
    report: DiagnosisReport
    report_degraded: bool = False
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_identity_and_timestamps(self) -> SessionCheckpoint:
        """校验 run/session 身份、时区、时间单调性和稳定引用集合。

        source run 必须与 session 不同名且两个时间均带时区；引用集合不允许重复。内容冲突由
        AgentState/领域模型负责，checkpoint 不静默删除或重排来源事实。
        """

        if self.source_run_id == self.session_id:
            raise ValueError("checkpoint source_run_id must differ from session_id")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("checkpoint timestamps must include a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("checkpoint updated_at cannot precede created_at")
        if len(self.observation_refs) != len(set(self.observation_refs)):
            raise ValueError("checkpoint observation_refs must not contain duplicates")
        return self


def build_session_checkpoint(
    result: DiagnosisRunResult,
    *,
    checkpoint_version: int,
    created_at: datetime,
    updated_at: datetime,
) -> SessionCheckpoint:
    """从已完成顶层结果构造只含公开字段的下一轮恢复快照。

    输入必须是完整 ``DiagnosisRunResult``；函数以报告终态 AgentState 为事实来源，确保 Auditor
    返工后的报告和最终 Evidence 一起保存。旧 checkpoint 上下文不会递归嵌套进新快照，因为
    ``session_context`` 不在持久字段中；报告本身已经是滚动后的最新公开摘要。
    """

    state = result.report.state
    report = state.draft_report
    if report is None:
        raise ValueError("completed diagnosis result requires a report for checkpointing")
    if checkpoint_version < 1:
        raise ValueError("checkpoint_version must be at least one")

    # 只复制恢复所需集合；显式字段清单是防止未来 AgentState 新增敏感字段后被自动持久化的门禁。
    return SessionCheckpoint(
        checkpoint_version=checkpoint_version,
        session_id=state.session_id,
        source_run_id=state.run_id,
        source_user_query=state.user_query,
        plan=_bounded_tail(state.plan, CHECKPOINT_MAX_PLAN_ITEMS),
        hypotheses=_bounded_tail(state.hypotheses, CHECKPOINT_MAX_HYPOTHESES),
        evidence=_bounded_tail(state.evidence, CHECKPOINT_MAX_EVIDENCE),
        tool_events=_bounded_tail(state.tool_events, CHECKPOINT_MAX_TOOL_EVENTS),
        retrieved_paths=_bounded_tail(state.retrieved_paths, CHECKPOINT_MAX_PATHS),
        observation_refs=_bounded_unique_tail(
            state.observation_refs,
            CHECKPOINT_MAX_OBSERVATION_REFS,
        ),
        report=report,
        report_degraded=result.report.outcome is ReportWorkflowOutcome.DEGRADED,
        created_at=created_at,
        updated_at=updated_at,
    )


def _bounded_tail(values: list[object] | tuple[object, ...], limit: int) -> tuple[object, ...]:
    """保留序列最新元素，并以 tuple 形成不可变 JSON/Pydantic 输入。

    诊断循环按时间追加 plan、Evidence 和 ToolEvent，因此尾部代表最新上下文；
    ``limit`` 在模块常量中集中定义，避免各调用点散落魔法数字。空序列和恰好
    达到上限时保持内容不变，超过上限时确定性丢弃最旧元素。
    """

    if limit < 1:
        raise ValueError("checkpoint item limit must be positive")
    return tuple(values[-limit:])


def _bounded_unique_tail(values: list[str] | tuple[str, ...], limit: int) -> tuple[str, ...]:
    """在滚动窗口内保留最新且唯一的 observation 引用。

    从后向前去重能保留最近一次出现的位置；随后反转恢复时间顺序，满足
    ``SessionCheckpoint`` 的唯一性校验，并避免重复引用浪费 JSONB 预算。
    """

    if limit < 1:
        raise ValueError("checkpoint reference limit must be positive")
    unique_reversed: list[str] = []
    seen: set[str] = set()
    for value in reversed(values):
        if value not in seen:
            unique_reversed.append(value)
            seen.add(value)
        if len(unique_reversed) == limit:
            break
    return tuple(reversed(unique_reversed))


def restore_agent_state(
    checkpoint: SessionCheckpoint | None,
    *,
    run_id: str,
    session_id: str,
    user_query: str,
) -> AgentState:
    """为新消息创建全新 run 状态，并按需继承上一轮公开会话上下文。

    无 checkpoint 时返回最小初始状态；有快照时先验证 session 所有权，再复制证据和工具事件。
    本轮 ``react_step`` 从零开始，旧 next_action/stop_reason/audit/memory 不恢复，因此单轮预算与
    终态不会污染追问；保留 ToolEvent 让 ReAct 从历史 Action 重建跨 run 去重指纹。
    """

    normalized_query = user_query.strip()
    if not normalized_query:
        raise ValueError("restored AgentState user_query must not be blank")
    if checkpoint is None:
        return AgentState(run_id=run_id, session_id=session_id, user_query=normalized_query)
    if checkpoint.session_id != session_id:
        raise ValueError("checkpoint cannot be restored into a different session")

    context = SessionTurnContext(
        source_run_id=checkpoint.source_run_id,
        previous_user_query=checkpoint.source_user_query,
        report_summary=checkpoint.report.summary,
        root_causes=list(checkpoint.report.root_causes),
        remediation_steps=list(checkpoint.report.remediation_steps),
        risks=list(checkpoint.report.risks),
        uncertainties=list(checkpoint.report.uncertainties),
        evidence_refs=list(checkpoint.report.evidence_refs),
        report_degraded=checkpoint.report_degraded,
    )
    # 所有列表都重新分配，防止新 run 的节点原地修改 checkpoint 所持有的集合引用。
    return AgentState(
        run_id=run_id,
        session_id=session_id,
        user_query=normalized_query,
        session_context=context,
        plan=list(checkpoint.plan),
        hypotheses=list(checkpoint.hypotheses),
        evidence=list(checkpoint.evidence),
        tool_events=list(checkpoint.tool_events),
        retrieved_paths=list(checkpoint.retrieved_paths),
        observation_refs=list(checkpoint.observation_refs),
        react_step=0,
        next_action=None,
        stop_reason=None,
        draft_report=None,
        audit_result=None,
        retry_count=0,
        memory_candidate=None,
    )


def build_checkpoint_retrieval_query(
    user_query: str,
    checkpoint: SessionCheckpoint | None,
    *,
    max_chars: int = 4000,
) -> str:
    """把当前追问与上一轮公开报告组合成 GraphRAG 检索查询。

    当前问题始终排在首位；有 checkpoint 时追加上一问题、报告摘要和根因，帮助“这个风险高吗”
    等省略表达恢复主题。函数只组合公开文本并按字符截断，不加入工具原始响应或高维向量。
    """

    normalized_query = user_query.strip()
    if not normalized_query:
        raise ValueError("checkpoint retrieval query must not be blank")
    if not 256 <= max_chars <= 20_000:
        raise ValueError("checkpoint retrieval max_chars must be between 256 and 20000")

    segments = [f"当前问题: {normalized_query}"]
    if checkpoint is not None:
        segments.extend(
            [
                f"上一问题: {checkpoint.source_user_query}",
                f"上一报告摘要: {checkpoint.report.summary}",
            ]
        )
        segments.extend(
            f"上一轮根因: {root_cause.root_cause}"
            for root_cause in checkpoint.report.root_causes
        )
    return "\n".join(segments)[:max_chars].rstrip()
