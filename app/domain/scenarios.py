"""合成场景与 Golden Case 的版本化数据结构。

ScenarioFixture 描述工具在指定 scenario_id 下的确定性响应；GoldenCaseSpec 描述预期的
诊断行为。两者分离后可以独立演进工具 Mock 和 Agent 评测标准。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import Component, RiskLevel
from app.domain.tooling import McpToolRequest, McpToolResponse, ToolName


class ScenarioToolResult(BaseModel):
    """绑定一个白名单工具、预期请求和该合成场景下的确定性响应。

    请求与响应都经过统一 MCP 契约校验，使 Fixture 不需要为每个组件建立松散 Schema；服务端
    按工具名和资源 ID 精确查找该记录，未命中时返回标准化错误而不是猜测结果。
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: ToolName
    request: McpToolRequest
    response: McpToolResponse


class ScenarioFixture(BaseModel):
    """描述一个可重放故障场景及其全部合成工具观察。

    场景元数据支持演示和评测，tool_results 则是 MCP Mock 的唯一事实来源。模型校验每个请求
    引用当前 scenario_id，并禁止同一工具/资源重复定义，确保查找结果唯一确定。
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    components: list[Component] = Field(min_length=1)
    expected_behavior: str = Field(min_length=1, max_length=1000)
    tool_results: list[ScenarioToolResult] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario_references(self) -> ScenarioFixture:
        """检查工具请求归属当前场景，并保证工具与资源组合没有重复。

        第一项约束防止复制 Fixture 时遗留错误 scenario_id；第二项约束保证仓储按二元键查找时
        只有一个响应。任一不变量破坏都会在启动加载阶段抛出 ValueError，而非运行时随机选取。
        """

        seen_calls: set[tuple[ToolName, str]] = set()
        for result in self.tool_results:
            # 先验证跨对象引用，再建立唯一键；这样错误消息能准确指出归属问题。
            if result.request.scenario_id != self.scenario_id:
                raise ValueError("tool request scenario_id must match fixture scenario_id")
            call_key = (result.tool_name, result.request.resource_id)
            if call_key in seen_calls:
                raise ValueError("fixture contains a duplicate tool/resource call")
            seen_calls.add(call_key)
        return self


class GoldenCaseSpec(BaseModel):
    """定义一个诊断评测案例的输入、必要行动和允许结果边界。

    Golden Case 不复制工具返回，而是引用 scenario_id 并描述预期意图、必要工具、证据来源、
    可接受根因和停止原因；这种分离使 Mock 数据与 Agent 策略可以独立演进和消融。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^golden_[a-z0-9][a-z0-9_-]{2,79}$")
    user_query: str = Field(min_length=1, max_length=4000)
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    expected_intent: str = Field(min_length=1, max_length=100)
    required_tools: list[ToolName] = Field(default_factory=list)
    allowed_root_causes: list[str] = Field(default_factory=list)
    required_evidence_sources: list[str] = Field(default_factory=list)
    expected_stop_reasons: list[str] = Field(min_length=1)
    expected_risk_level: RiskLevel
