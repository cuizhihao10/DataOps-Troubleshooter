"""公开 LangGraph 编排层复用的唯一领域 AgentState。

该稳定导入边界让图节点、未来 checkpoint 和 API 共用同一 Pydantic 状态模型，避免在模块之间
传递松散字典；模型刻意排除 Thought 和 reasoning_process，保证持久化内容可公开审计。
"""

from app.domain.models import AgentState

__all__ = ["AgentState"]
