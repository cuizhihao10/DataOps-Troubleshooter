from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import Component, RiskLevel
from app.domain.tooling import McpToolRequest, McpToolResponse, ToolName


class ScenarioToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: ToolName
    request: McpToolRequest
    response: McpToolResponse


class ScenarioFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    components: list[Component] = Field(min_length=1)
    expected_behavior: str = Field(min_length=1, max_length=1000)
    tool_results: list[ScenarioToolResult] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario_references(self) -> ScenarioFixture:
        seen_calls: set[tuple[ToolName, str]] = set()
        for result in self.tool_results:
            if result.request.scenario_id != self.scenario_id:
                raise ValueError("tool request scenario_id must match fixture scenario_id")
            call_key = (result.tool_name, result.request.resource_id)
            if call_key in seen_calls:
                raise ValueError("fixture contains a duplicate tool/resource call")
            seen_calls.add(call_key)
        return self


class GoldenCaseSpec(BaseModel):
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
