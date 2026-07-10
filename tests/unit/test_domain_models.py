"""验证 AgentState 的安全默认值、可序列化性和思维链排除约束。

领域状态是未来 LangGraph 的共享载体；本测试确保新增字段不会意外引入 Thought 或
reasoning_process，也保证空状态能稳定序列化供 checkpoint 使用。
"""

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
