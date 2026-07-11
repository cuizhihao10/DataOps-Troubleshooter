"""验证资源化诊断 API 的 message 路由、run 状态机和事件连续性领域约束。

测试不依赖 FastAPI 或 PostgreSQL，直接构造 Pydantic 模型证明无效输入在任何 I/O 前失败；成功模型
则可由仓储与 HTTP 层共享，不需要重复手写状态规则。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.capabilities import DiagnosisIntent, HistoryTrigger
from app.domain.models import Component
from app.orchestration.run_models import (
    DIAGNOSIS_API_CONTRACT_ID,
    AgentRunSnapshot,
    AgentRunStatus,
    DiagnosisMessage,
    RunEventList,
    RunEventPhase,
    RunPublicEvent,
)

NOW = datetime(2026, 7, 15, 11, 0, tzinfo=UTC)


def _running_snapshot() -> AgentRunSnapshot:
    """构造字段完整且没有终态 payload 的 running run 快照。

    intent/components 满足单组件 capability 路由，所有时间相同且带 UTC；辅助对象用于分别破坏
    status、result/error 和时间边界，不访问数据库。
    """

    return AgentRunSnapshot(
        run_id="run_1111111111111111",
        session_id="session_2222222222222222",
        status=AgentRunStatus.RUNNING,
        user_query="检查 LTS 合成任务",
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
        history_trigger=HistoryTrigger.NOT_REQUESTED,
        created_at=NOW,
        started_at=NOW,
        updated_at=NOW,
    )


def _event(*, sequence: int, event_id: str) -> RunPublicEvent:
    """构造属于固定 run 的安全 system 事件，并允许覆盖序号和合法 ID。

    payload 只含空字典，确保事件列表测试只关注 run/sequence 不变量；created_at 使用 UTC，避免失败
    被时间校验抢先触发。
    """

    return RunPublicEvent(
        event_id=event_id,
        run_id="run_1111111111111111",
        sequence=sequence,
        phase=RunEventPhase.SYSTEM,
        event_type="synthetic_event",
        summary="合成公开事件。",
        created_at=NOW,
    )


def test_message_reuses_capability_component_scope_validation() -> None:
    """验证 API message 与 capability registry 共享单/跨组件数量和重复组件规则。

    合法单组件消息可投影为 CapabilitySelectionRequest；跨组件意图只给一个组件、或重复 LTS 组件时
    均在模型构造阶段失败，不创建 run 或等待 LangGraph 再报错。
    """

    message = DiagnosisMessage(
        content="检查 LTS 合成任务",
        intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
        components=(Component.LTS,),
    )
    assert message.capability_request().components == (Component.LTS,)

    with pytest.raises(ValidationError, match="cross-component diagnosis"):
        DiagnosisMessage(
            content="检查跨组件链路",
            intent=DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
        )
    with pytest.raises(ValidationError, match="components must not contain duplicates"):
        DiagnosisMessage(
            content="检查重复组件",
            intent=DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS,
            components=(Component.LTS, Component.LTS),
        )


def test_run_snapshot_rejects_terminal_payload_or_time_mismatch() -> None:
    """验证 running/completed/failed 的 result/error/completed_at 组合不能互相混用。

    running 携带完成时间、completed 缺少 result、以及 started 早于 created 都应失败；错误在 JSONB
    写入或 API 返回前暴露，数据库 CheckConstraint 仍提供最后一道防线。
    """

    running = _running_snapshot()
    assert running.status is AgentRunStatus.RUNNING

    with pytest.raises(ValidationError, match="running run cannot contain"):
        AgentRunSnapshot.model_validate({**running.model_dump(), "completed_at": NOW})
    with pytest.raises(ValidationError, match="completed run requires result"):
        AgentRunSnapshot.model_validate(
            {
                **running.model_dump(),
                "status": AgentRunStatus.COMPLETED,
                "completed_at": NOW,
            }
        )
    with pytest.raises(ValidationError, match="timestamps must be monotonic"):
        AgentRunSnapshot.model_validate(
            {
                **running.model_dump(),
                "created_at": NOW,
                "started_at": datetime(2026, 7, 15, 10, 59, tzinfo=UTC),
            }
        )


def test_event_list_rejects_gaps_and_accepts_consecutive_sequence() -> None:
    """验证 `/events` 响应只接受同 run 且从一连续递增的时间线。

    合法 1、2 序列通过；1、3 缺口失败且模型不自动重排。该边界让前端和评测能可靠按 sequence
    重放控制流，而不是猜测数据库遗漏或重复。
    """

    first = _event(sequence=1, event_id="run_evt_1111111111111111")
    second = _event(sequence=2, event_id="run_evt_2222222222222222")
    result = RunEventList(
        contract_id=DIAGNOSIS_API_CONTRACT_ID,
        run_id=first.run_id,
        events=(first, second),
    )
    assert [event.sequence for event in result.events] == [1, 2]

    with pytest.raises(ValidationError, match="sequence must be consecutive"):
        RunEventList(
            contract_id=DIAGNOSIS_API_CONTRACT_ID,
            run_id=first.run_id,
            events=(first, _event(sequence=3, event_id="run_evt_3333333333333333")),
        )
