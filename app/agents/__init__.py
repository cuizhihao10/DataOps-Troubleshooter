"""公开 Planner 与 Auditor 两个独立 LLM Agent 的代码边界。

两个角色都使用强类型异步协议、版本化 Prompt 与 OpenAI-compatible Structured Outputs；检索、
MCP、报告修订和状态回写仍由确定性节点负责，不会被包装成额外 Agent。
"""

from app.agents.auditor import AuditorAgent, AuditorAgentError, AuditorTurnContext
from app.agents.auditor_adapter import OpenAICompatibleAuditorAgent
from app.agents.auditor_prompting import AuditorPromptBundle, AuditorPromptRenderer
from app.agents.planner import PlannerAgent, PlannerAgentError, PlannerTurnContext
from app.agents.planner_adapter import OpenAICompatiblePlannerAgent
from app.agents.prompting import PlannerPromptBundle, PlannerPromptRenderer

__all__ = [
    "AuditorAgent",
    "AuditorAgentError",
    "AuditorPromptBundle",
    "AuditorPromptRenderer",
    "AuditorTurnContext",
    "OpenAICompatibleAuditorAgent",
    "OpenAICompatiblePlannerAgent",
    "PlannerAgent",
    "PlannerAgentError",
    "PlannerPromptBundle",
    "PlannerPromptRenderer",
    "PlannerTurnContext",
]
