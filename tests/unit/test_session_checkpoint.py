"""验证短期会话 checkpoint 的安全快照、隔离、恢复重置和检索补全规则。

这些测试不连接 PostgreSQL；数据库原子性由 postgres marker 集成测试覆盖。本文件专注证明快照
只含公开字段，新 run 不继承旧终态/预算，并且省略式追问可以使用上一轮报告补全检索主题。
"""

from datetime import UTC, datetime

import pytest

from app.domain.models import DiagnosisReport, RootCauseConclusion
from app.memory.checkpoint import (
    SessionCheckpoint,
    build_checkpoint_retrieval_query,
    restore_agent_state,
)

NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)


def _checkpoint() -> SessionCheckpoint:
    """构造一份只含公开报告和根因引用的最小 v1 checkpoint。

    ID、时间和文本均为合成数据；快照不含 ToolEvent 以隔离状态恢复规则，跨 trace 去重由 ReAct
    专门测试覆盖。报告引用允许没有内嵌 Evidence，因为本测试不执行 Auditor 规则。
    """

    return SessionCheckpoint(
        checkpoint_version=1,
        session_id="session_1111111111111111",
        source_run_id="run_2222222222222222",
        source_user_query="为什么 LTS 合成任务失败？",
        report=DiagnosisReport(
            summary="上一轮确认合成上游数据尚未就绪。",
            root_causes=[
                RootCauseConclusion(
                    root_cause="合成上游数据未就绪",
                    confidence=0.9,
                    evidence_refs=["ev_synthetic_upstream"],
                )
            ],
            evidence_refs=["ev_synthetic_upstream"],
            risks=["直接重跑可能再次失败。"],
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def test_restore_creates_new_run_and_resets_transient_fields() -> None:
    """验证追问继承公开报告上下文，但使用新 run 身份和全新单轮控制状态。

    恢复结果应包含上一轮来源、摘要和根因；react_step、next_action、stop_reason、草稿、审计和记忆
    候选都回到初始值，证明 checkpoint 不会让旧终态跳过本轮 Planner/Auditor。
    """

    state = restore_agent_state(
        _checkpoint(),
        run_id="run_3333333333333333",
        session_id="session_1111111111111111",
        user_query="这个操作风险高吗？",
    )

    assert state.run_id == "run_3333333333333333"
    assert state.user_query == "这个操作风险高吗？"
    assert state.session_context is not None
    assert state.session_context.source_run_id == "run_2222222222222222"
    assert state.session_context.root_causes[0].root_cause == "合成上游数据未就绪"
    assert state.react_step == 0
    assert state.next_action is None
    assert state.stop_reason is None
    assert state.draft_report is None
    assert state.audit_result is None
    assert state.memory_candidate is None


def test_restore_rejects_cross_session_checkpoint() -> None:
    """验证任何 checkpoint 都不能恢复到另一 session，即使调用方持有有效对象。

    session_id 是短期记忆的隔离边界；不做自动改写或复制，错误在 Planner、检索和数据库外部 I/O
    之前以 ValueError 暴露，防止会话 A 的根因进入会话 B。
    """

    with pytest.raises(ValueError, match="different session"):
        restore_agent_state(
            _checkpoint(),
            run_id="run_4444444444444444",
            session_id="session_aaaaaaaaaaaaaaaa",
            user_query="继续",
        )


def test_checkpoint_retrieval_query_prioritizes_followup_and_adds_report_context() -> None:
    """验证 GraphRAG 查询先保留当前追问，再追加上一轮问题、摘要和根因。

    该排序保证字符预算紧张时不会把用户新问题挤出；上一轮公开报告补全省略主题，使检索不必仅靠
    “这个风险高吗”猜测对象。函数不会加入 Prompt、Thought 或数据库内部字段。
    """

    query = build_checkpoint_retrieval_query("这个操作风险高吗？", _checkpoint())

    assert query.startswith("当前问题: 这个操作风险高吗？")
    assert "上一问题: 为什么 LTS 合成任务失败？" in query
    assert "上一报告摘要: 上一轮确认合成上游数据尚未就绪。" in query
    assert "上一轮根因: 合成上游数据未就绪" in query
