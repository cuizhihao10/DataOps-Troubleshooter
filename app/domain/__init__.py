"""跨 API、Agent、MCP、检索和持久化共享的领域契约出口。

统一出口让调用方依赖稳定模型而不是松散字典。具体模型仍按工具、Planner 和报告拆分，
避免单文件演变为与外部框架耦合的通用数据容器。
"""

from app.domain.models import (
    AgentState,
    CaseMemory,
    DiagnosisReport,
    Evidence,
    FaultHypothesis,
    ToolEvent,
)
from app.domain.planner import PlannerDecision
from app.domain.scenarios import GoldenCaseSpec, ScenarioFixture
from app.domain.tooling import McpToolRequest, McpToolResponse, ToolName

__all__ = [
    "AgentState",
    "CaseMemory",
    "DiagnosisReport",
    "Evidence",
    "FaultHypothesis",
    "GoldenCaseSpec",
    "McpToolRequest",
    "McpToolResponse",
    "PlannerDecision",
    "ScenarioFixture",
    "ToolEvent",
    "ToolName",
]
