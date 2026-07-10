"""验证 MCP 响应转换为 Evidence、ToolEvent 和稳定引用。

Observation 适配器是模型事实来源的关键边界。本测试检查证据元数据、trace、事件列表和
重试属性，确保后续 Planner 只能引用真实工具返回。
"""

from datetime import UTC, datetime, timedelta

from app.domain.planner import ToolAction
from app.domain.tooling import McpToolResponse
from app.mcp.observation import normalize_observation


def test_successful_tool_result_becomes_evidence_and_event() -> None:
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
