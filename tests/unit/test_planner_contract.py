import json

import pytest
from pydantic import ValidationError

from app.agents.prompts import PLANNER_PROMPT_ID, load_planner_prompt
from app.domain.planner import PlannerDecision

VALID_ACTION = {
    "tool_name": "lts.get_task_status",
    "arguments": {
        "resource_id": "dws_order_report_daily",
        "time_range": {
            "start": "2026-07-10T00:00:00+08:00",
            "end": "2026-07-10T03:00:00+08:00",
        },
        "scenario_id": "cross_chain_pk_conflict",
        "trace_id": "trace_cross_001",
    },
}


def test_call_tool_decision_requires_action() -> None:
    decision = PlannerDecision.model_validate(
        {
            "status": "call_tool",
            "decision_summary": "先确认 LTS 当前状态。",
            "hypothesis_updates": [],
            "action": VALID_ACTION,
            "evidence_refs": [],
            "stop_reason": None,
        }
    )
    assert decision.action is not None
    assert decision.action.tool_name.value == "lts.get_task_status"


@pytest.mark.parametrize("status", ["finish", "need_user_input"])
def test_stopping_decision_requires_stop_reason(status: str) -> None:
    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": status,
                "decision_summary": "停止当前调查。",
                "hypothesis_updates": [],
                "action": None,
                "evidence_refs": [],
                "stop_reason": None,
            }
        )


def test_non_call_tool_decision_rejects_action() -> None:
    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": "finish",
                "decision_summary": "证据充分。",
                "hypothesis_updates": [],
                "action": VALID_ACTION,
                "evidence_refs": ["ev_001"],
                "stop_reason": "evidence_sufficient",
            }
        )


def test_planner_schema_does_not_expose_reasoning_fields() -> None:
    schema = json.dumps(PlannerDecision.model_json_schema()).lower()
    assert '"thought"' not in schema
    assert "reasoning_process" not in schema


def test_versioned_prompt_contains_required_runtime_placeholders() -> None:
    prompt = load_planner_prompt()
    assert PLANNER_PROMPT_ID == "planner-react:v1"
    for placeholder in (
        "{user_query}",
        "{hypotheses}",
        "{evidence_bundle}",
        "{tool_schemas}",
        "{max_react_steps}",
    ):
        assert placeholder in prompt
