"""合成场景与 Golden Case 的版本化数据结构。

ScenarioFixture 描述工具在指定 scenario_id 下的确定性响应；GoldenCaseSpec 描述预期的
诊断行为。两者分离后可以独立演进工具 Mock 和 Agent 评测标准。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

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


GoldenRelationType = Literal[
    "RUNS_ON",
    "DEPENDS_ON",
    "PRODUCES",
    "CONSUMES",
    "MANIFESTS_AS",
    "CAUSED_BY",
    "RESOLVED_BY",
    "SIMILAR_TO",
]
GoldenNodeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")]


class GoldenCaseCategory(StrEnum):
    """对应产品设计 28 条 Golden Cases 的五类互斥配额。

    显式类别让数据集扩展能够按 8/10/4/3/3 目标审计，而不是只增加总数；字符串枚举可稳定进入
    JSON、评测报告和文档快照。案例只能属于一类，跨类别能力可由工具、路径和标签继续表达。
    """

    SINGLE_COMPONENT = "single_component"
    CROSS_COMPONENT = "cross_component"
    AMBIGUOUS_OR_INSUFFICIENT = "ambiguous_or_insufficient"
    TOOL_ANOMALY_OR_CONFLICT = "tool_anomaly_or_conflict"
    MEMORY_RECALL = "memory_recall"


class GoldenFaultPathRequirement(BaseModel):
    """描述 Golden Case 必须识别并在最终报告中使用的一条有序图路径。

    节点与关系均使用人工知识图的稳定 ID/白名单关系；评测允许实际路径包含额外中间实体，但要求
    标注顺序保持一致。``path_label`` 只用于失败报告定位，不参与匹配或自然语言近似判断。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path_label: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    required_node_ids: list[GoldenNodeId] = Field(min_length=2, max_length=3)
    required_relation_types: list[GoldenRelationType] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_path_shape(self) -> GoldenFaultPathRequirement:
        """保证 N 个节点恰好由 N-1 条关系连接，并拒绝重复节点形成伪路径。

        当前 GraphRAG 最多扩展两跳，所以模型最多接受三个节点；关系数量不匹配或节点重复会在
        Fixture 加载阶段失败，避免评分器猜测缺失边或把环路当作故障链。
        """

        if len(self.required_relation_types) != len(self.required_node_ids) - 1:
            raise ValueError("Golden fault path relations must connect every adjacent node")
        if len(self.required_node_ids) != len(set(self.required_node_ids)):
            raise ValueError("Golden fault path nodes must not contain duplicates")
        return self


class GoldenMemoryExpectation(BaseModel):
    """描述一条记忆 Golden Case 必须召回的 confirmed 历史案例。

    memory ID、历史根因和固定相似度用于构造可重复强类型上下文；``expect_root_conflict`` 标记旧根因
    是否与本次允许根因不同，使评测器能单独验证实时 Observation 优先，而不把相似度当作事实。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(pattern=r"^mem_[a-z0-9][a-z0-9_-]{2,95}$")
    historical_root_cause: str = Field(min_length=1, max_length=1000)
    similarity: float = Field(gt=0, le=1)
    expect_root_conflict: bool = False


class GoldenHistoryExpectation(BaseModel):
    """封装记忆类别案例的必要召回、禁止命中与实时优先要求。

    ``required_memories`` 至少一条且只代表 confirmed 候选；forbidden ID 用于拦截错误召回或状态污染。
    报告必须按原顺序投影召回案例，冲突案例还必须让本次 TOOL Evidence 支持的根因优先。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_memories: list[GoldenMemoryExpectation] = Field(min_length=1, max_length=5)
    forbidden_memory_ids: list[str] = Field(default_factory=list)
    require_realtime_priority: bool = True

    @model_validator(mode="after")
    def validate_memory_identity_sets(self) -> GoldenHistoryExpectation:
        """拒绝重复或同时 required/forbidden 的 memory ID，保持召回分母无歧义。

        历史根因允许重复，因为多个已确认案例可能属于同一故障；身份集合冲突则会让任何结果同时
        成功和失败，因此必须在 Fixture 加载阶段阻断。
        """

        required_ids = [memory.memory_id for memory in self.required_memories]
        if len(required_ids) != len(set(required_ids)):
            raise ValueError("Golden history required memory IDs must be unique")
        if len(self.forbidden_memory_ids) != len(set(self.forbidden_memory_ids)):
            raise ValueError("Golden history forbidden memory IDs must be unique")
        if set(required_ids) & set(self.forbidden_memory_ids):
            raise ValueError("Golden history required and forbidden memory IDs must not overlap")
        return self


class GoldenCaseSpec(BaseModel):
    """定义一个诊断评测案例的输入、必要行动和允许结果边界。

    Golden Case 不复制工具返回，而是引用 scenario_id 并描述预期意图、必要工具、图路径、证据来源、
    可接受根因和停止原因；这种分离使 Mock 数据、GraphRAG 与 Agent 策略可以独立演进和消融。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: Literal["golden-case:v4"]
    case_id: str = Field(pattern=r"^golden_[a-z0-9][a-z0-9_-]{2,79}$")
    case_category: GoldenCaseCategory
    user_query: str = Field(min_length=1, max_length=4000)
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    expected_intent: str = Field(min_length=1, max_length=100)
    required_tools: list[ToolName] = Field(default_factory=list)
    required_fault_paths: list[GoldenFaultPathRequirement] = Field(default_factory=list)
    history_expectation: GoldenHistoryExpectation | None = None
    allowed_root_causes: list[str] = Field(default_factory=list)
    required_evidence_sources: list[str] = Field(default_factory=list)
    expected_stop_reasons: list[str] = Field(min_length=1)
    expected_risk_level: RiskLevel

    @model_validator(mode="after")
    def validate_unique_requirements(self) -> GoldenCaseSpec:
        """拒绝重复工具、路径标签、证据来源和允许答案，保持每项评测分母唯一。

        列表顺序用于失败明细和宏观重放，但同一要求出现两次会人为降低或提高覆盖率，因此在加载
        阶段直接失败。空路径/根因集合仍合法，用于工具异常和证据不足的安全降级案例。
        """

        for field_name in (
            "required_tools",
            "allowed_root_causes",
            "required_evidence_sources",
            "expected_stop_reasons",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"Golden case {field_name} must not contain duplicates")
        path_labels = [path.path_label for path in self.required_fault_paths]
        if len(path_labels) != len(set(path_labels)):
            raise ValueError("Golden case fault path labels must be unique")
        is_memory_case = self.case_category is GoldenCaseCategory.MEMORY_RECALL
        if is_memory_case != (self.history_expectation is not None):
            raise ValueError(
                "Golden memory category and history_expectation must be present together"
            )
        if self.history_expectation is not None:
            allowed_roots = set(self.allowed_root_causes)
            for memory in self.history_expectation.required_memories:
                actual_conflict = memory.historical_root_cause not in allowed_roots
                if memory.expect_root_conflict != actual_conflict:
                    raise ValueError(
                        "Golden history conflict flag must match allowed root annotations"
                    )
        return self
