"""验证五项固定运行时 capability 的注册、选择、合并和失败边界。

测试只操作强类型配置，不启动模型或工具，证明 capability 是确定性策略层。覆盖单/跨组件、
历史按需触发、实时证据优先级和非法组件范围，防止后续把注册表扩张成动态 Agent 系统。
"""

import pytest
from pydantic import ValidationError

from app.capabilities import (
    CAPABILITY_CONTRACT_ID,
    CapabilityDefinition,
    CapabilityInputField,
    CapabilityName,
    CapabilityRegistry,
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
)
from app.domain.models import Component
from app.domain.tooling import ToolName


def test_registry_contains_exactly_the_five_product_capabilities() -> None:
    """验证默认注册表的名称、顺序和契约版本与产品基线完全一致。

    测试从公开 API 读取冻结定义，不依赖模块私有常量；如果实现者新增、删除或重排能力，断言
    会失败并要求先更新产品契约，而不是让未审计策略静默进入 Planner 上下文。
    """

    registry = CapabilityRegistry()

    assert registry.contract_id == CAPABILITY_CONTRACT_ID
    assert tuple(definition.name for definition in registry.definitions()) == tuple(CapabilityName)
    assert set(CapabilityDefinition.model_fields) == {
        "name",
        "summary",
        "prompt_fragment",
        "tool_priority",
        "required_inputs",
        "output_validation_rules",
    }


def test_single_component_selection_filters_tools_and_adds_mandatory_outputs() -> None:
    """验证 BDS 单组件意图只暴露 BDS 工具并始终追加风险与报告能力。

    请求未触发历史匹配，因此结果应恰好含三项能力；工具优先级必须保持状态、日志、表信息
    顺序且不能出现 LTS/FlashSync，证明注册表不会诱导 Planner 无依据扩大调查范围。
    """

    selection = CapabilityRegistry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.BDS,),
        )
    )

    assert selection.active_capabilities == (
        CapabilityName.SINGLE_COMPONENT_DIAGNOSIS,
        CapabilityName.RISK_ASSESSMENT,
        CapabilityName.STRUCTURED_REPORTING,
    )
    assert selection.tool_priority == (
        ToolName.BDS_GET_TASK_STATUS,
        ToolName.BDS_GET_TASK_LOG,
        ToolName.BDS_GET_TABLE_INFO,
    )
    assert CapabilityInputField.REALTIME_OBSERVATIONS in selection.required_inputs
    assert len(selection.prompt_fragments) == len(selection.active_capabilities)


def test_cross_component_selection_adds_history_only_for_explicit_trigger() -> None:
    """验证跨组件调查在显式用户触发时按固定位置加入历史案例能力。

    历史匹配置于主调查之后、风险和报告之前，九个只读工具按链路调查顺序去重保留；结果同时
    记录触发来源，使后续事件能说明为何本轮读取案例而不是把召回当成默认步骤。
    """

    selection = CapabilityRegistry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS,
            components=(Component.LTS, Component.BDS, Component.FLASHSYNC),
            history_trigger=HistoryTrigger.USER_REQUESTED,
        )
    )

    assert selection.active_capabilities == (
        CapabilityName.CROSS_COMPONENT_CHAIN_TRACING,
        CapabilityName.HISTORY_CASE_MATCHING,
        CapabilityName.RISK_ASSESSMENT,
        CapabilityName.STRUCTURED_REPORTING,
    )
    assert selection.history_trigger is HistoryTrigger.USER_REQUESTED
    assert len(selection.tool_priority) == len(ToolName)
    assert len(selection.tool_priority) == len(set(selection.tool_priority))


@pytest.mark.parametrize(
    "history_trigger",
    [
        HistoryTrigger.USER_REQUESTED,
        HistoryTrigger.PLANNER_VALIDATION,
        HistoryTrigger.REUSABLE_SIGNATURE,
    ],
)
def test_every_approved_history_trigger_preserves_realtime_evidence_priority(
    history_trigger: HistoryTrigger,
) -> None:
    """验证三类批准触发都启用相同的确认状态和实时证据优先规则。

    参数化确保未来修改其中一个触发分支时不会遗漏安全语义；Prompt 和输出规则都必须明确旧案例
    只作参考，若与本轮 Observation 冲突则以后者为准，防止相似度覆盖当前事实。
    """

    selection = CapabilityRegistry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
            components=(Component.LTS,),
            history_trigger=history_trigger,
        )
    )

    assert CapabilityName.HISTORY_CASE_MATCHING in selection.active_capabilities
    assert CapabilityInputField.CONFIRMED_CASES in selection.required_inputs
    combined_policy = "\n".join((*selection.prompt_fragments, *selection.output_validation_rules))
    assert "confirmed" in combined_policy
    assert "实时" in combined_policy
    assert "为准" in combined_policy


@pytest.mark.parametrize(
    ("intent", "components"),
    [
        (DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS, (Component.LTS, Component.BDS)),
        (DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS, (Component.LTS,)),
        (DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS, (Component.LTS, Component.LTS)),
    ],
)
def test_selection_request_rejects_ambiguous_component_scopes(
    intent: DiagnosisIntent,
    components: tuple[Component, ...],
) -> None:
    """验证单组件、多组件和重复组件之间的非法组合在路由边界被拒绝。

    测试期望 Pydantic ValidationError，说明错误不会被注册表猜测性修复；这能避免跨组件长度被
    重复值伪造，也避免单组件工具过滤面对多个目标时随机选择第一个组件。
    """

    with pytest.raises(ValidationError):
        CapabilitySelectionRequest(intent=intent, components=components)


def test_selection_serializes_as_planner_context_without_execution_hooks() -> None:
    """验证选择结果可直接 JSON 序列化且只包含声明式策略数据。

    JSON 模式应把枚举转换为稳定字符串，并且 Schema 不得出现 callable、handler、LLM 或 MCP
    客户端字段；该边界保证 capability 只是 Planner 输入契约，不会隐藏执行副作用。
    """

    selection = CapabilityRegistry().select(
        CapabilitySelectionRequest(
            intent=DiagnosisIntent.CROSS_COMPONENT_DIAGNOSIS,
            components=(Component.LTS, Component.BDS),
        )
    )
    payload = selection.model_dump(mode="json")

    assert payload["contract_id"] == "runtime-capabilities:v1"
    assert payload["components"] == ["lts", "bds"]
    assert all(not tool.startswith("flashsync.") for tool in payload["tool_priority"])
    serialized_schema = str(CapabilityDefinition.model_json_schema()).lower()
    for forbidden_field in ("handler", "callback", "llm_client", "mcp_client"):
        assert forbidden_field not in serialized_schema
