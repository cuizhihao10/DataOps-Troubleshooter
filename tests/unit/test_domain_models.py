"""验证 AgentState 的安全默认值、可序列化性和思维链排除约束。

领域状态是未来 LangGraph 的共享载体；本测试确保新增字段不会意外引入 Thought 或
reasoning_process，也保证空状态能稳定序列化供 checkpoint 使用。
"""

from app.domain.models import AgentState


def test_agent_state_is_serializable_without_reasoning_process() -> None:
    """验证最小 AgentState 可稳定 JSON 序列化且不存在原始思维链字段。

    只提供运行、会话和问题三个必填值，借此检查 ReAct 步数与重试次数安全默认值；序列化结果中
    同时拒绝 `reasoning_process` 和 `thought`，防止未来字段扩张把模型内部推理写入 checkpoint、
    日志、API 或长期记忆。
    """

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
