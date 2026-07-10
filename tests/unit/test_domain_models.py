from app.domain.models import AgentState


def test_agent_state_is_serializable_without_reasoning_process() -> None:
    state = AgentState(
        run_id="run_001",
        session_id="session_001",
        user_query="检查合成任务故障",
    )
    payload = state.model_dump(mode="json")

    assert payload["react_step"] == 0
    assert payload["retry_count"] == 0
    assert "reasoning_process" not in payload
    assert "thought" not in payload
