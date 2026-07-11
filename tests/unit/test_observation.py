"""验证 MCP 响应转换为 Evidence、ToolEvent 和稳定引用。

Observation 适配器是模型事实来源的关键边界。本测试检查证据元数据、trace、事件列表和
重试属性，确保后续 Planner 只能引用真实工具返回。
"""

from datetime import UTC, datetime, timedelta

from app.domain.planner import ToolAction
from app.domain.tooling import McpToolResponse
from app.mcp.observation import normalize_observation


def test_successful_tool_result_becomes_evidence_and_event() -> None:
    """验证成功 MCP 响应被确定性转换为一条 Evidence、一个 ToolEvent 和稳定引用。

    测试使用生产 ToolAction/Response Schema，传入明确开始完成时间后检查 trace、重试属性、事件数
    和工具元数据；这证明 Planner 后续引用来自真实响应，且 Observation 没有添加未返回的事实。
    """

    action = ToolAction.model_validate(
        {
            "tool_name": "lts.get_task_status",
            "arguments": {
                "resource_id": "dws_order_report_daily",
                "time_range": {
                    "start": "2026-07-10T00:00:00+08:00",
                    "end": "2026-07-10T03:00:00+08:00",
                },
                "scenario_id": "cross_chain_pk_conflict",
                "trace_id": "trace_observation_001",
            },
        }
    )
    response = McpToolResponse.model_validate(
        {
            "ok": True,
            "data": {"status": "failed"},
            "evidence": [
                {
                    "source_id": "lts_status_dws_order_report_daily",
                    "content": "任务失败，上游数据未就绪。",
                }
            ],
            "error_code": None,
            "error_message": None,
            "observed_at": "2026-07-10T03:01:00+08:00",
        }
    )
    started_at = datetime.now(UTC)
    observation = normalize_observation(
        action=action,
        response=response,
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        attempt=1,
    )

    assert len(observation.evidence) == 1
    assert observation.observation_refs == [observation.evidence[0].evidence_id]
    assert observation.tool_event.trace_id == "trace_observation_001"
    assert len(observation.tool_events) == 1
    assert observation.tool_event.retryable is False
    assert observation.evidence[0].metadata["tool_name"] == "lts.get_task_status"


def test_same_tool_with_different_parameters_gets_distinct_audit_ids() -> None:
    """验证同一 trace 中调用相同工具但参数不同不会复用 Evidence/Event ID。

    ReAct 允许在不同资源或时间窗上使用同一工具；请求身份必须进入稳定摘要，否则第二次合法
    Observation 会与第一次冲突。测试固定 source_id，只改变 resource_id，精确覆盖该边界。
    """

    first_action = ToolAction.model_validate(
        {
            "tool_name": "lts.get_task_status",
            "arguments": {
                "resource_id": "lts_task_a",
                "time_range": {
                    "start": "2026-07-10T00:00:00+08:00",
                    "end": "2026-07-10T03:00:00+08:00",
                },
                "scenario_id": "cross_chain_pk_conflict",
                "trace_id": "trace_distinct_requests_001",
            },
        }
    )
    second_action = first_action.model_copy(
        update={
            "arguments": first_action.arguments.model_copy(update={"resource_id": "lts_task_b"})
        }
    )
    response = McpToolResponse.model_validate(
        {
            "ok": True,
            "data": {"status": "failed"},
            "evidence": [
                {
                    "source_id": "shared_source_id",
                    "content": "合成状态观察。",
                }
            ],
            "observed_at": "2026-07-10T03:01:00+08:00",
        }
    )
    started_at = datetime.now(UTC)

    first = normalize_observation(
        action=first_action,
        response=response,
        started_at=started_at,
        completed_at=started_at,
        attempt=1,
    )
    second = normalize_observation(
        action=second_action,
        response=response,
        started_at=started_at,
        completed_at=started_at,
        attempt=1,
    )

    assert first.evidence[0].evidence_id != second.evidence[0].evidence_id
    assert first.tool_event.event_id != second.tool_event.event_id
