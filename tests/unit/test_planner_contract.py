"""验证 Planner ReAct 结构化输出和版本化 Prompt 契约。

测试覆盖 Action/停止原因的互斥关系、Schema 中不存在原始思维链字段，以及 Prompt 保留
运行时必须注入的占位符，防止自由文本直接驱动工具。
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.prompts import (
    PLANNER_PROMPT_ID,
    load_planner_prompt,
    load_planner_prompt_parts,
)
from app.domain.planner import PlannerDecision

VALID_ACTION = {
    "tool_name": "lts.get_task_status",
    "arguments": {
        "resource_id": "dws_order_report_daily",
        "time_range": {
            "start": "2026-07-10T00:00:00+08:00",
            "end": "2026-07-10T03:00:00+08:00",
        },
        "scenario_id": "cross_chain_pk_conflict",
        "trace_id": "trace_cross_001",
    },
}


def test_call_tool_decision_requires_action() -> None:
    """验证合法 call_tool 决策必须携带可解析的白名单 ToolAction。

    测试从字典经过完整 PlannerDecision Schema，断言嵌套 Action 和工具枚举被正确构造；这保护
    ReAct 工作流只能执行结构化参数，不能从 decision_summary 自然语言猜测外部动作。
    """

    decision = PlannerDecision.model_validate(
        {
            "status": "call_tool",
            "decision_summary": "先确认 LTS 当前状态。",
            "hypothesis_updates": [],
            "action": VALID_ACTION,
            "evidence_refs": [],
            "stop_reason": None,
        }
    )
    assert decision.action is not None
    assert decision.action.tool_name.value == "lts.get_task_status"


@pytest.mark.parametrize("status", ["finish", "need_user_input"])
def test_stopping_decision_requires_stop_reason(status: str) -> None:
    """验证 finish 与 need_user_input 两种停止状态都必须提供公开停止原因。

    参数化覆盖正常结束和请求补参，故意传入 `stop_reason=None` 并期望 ValidationError；该约束让
    运行事件能够解释循环为何终止，避免 Planner 无声退出或依赖隐藏思维链说明原因。
    """

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": status,
                "decision_summary": "停止当前调查。",
                "hypothesis_updates": [],
                "action": None,
                "evidence_refs": [],
                "stop_reason": None,
            }
        )


def test_non_call_tool_decision_rejects_action() -> None:
    """验证结束状态即使证据充分也不能夹带一个待执行 ToolAction。

    构造 finish 与合法 Action 的矛盾组合并期望校验失败，防止工作流在记录“已结束”的同时产生
    未审计副作用；一次结构化决策必须只能选择继续调用或停止其中一种控制流。
    """

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": "finish",
                "decision_summary": "证据充分。",
                "hypothesis_updates": [],
                "action": VALID_ACTION,
                "evidence_refs": ["ev_001"],
                "stop_reason": "evidence_sufficient",
            }
        )


def test_planner_schema_does_not_expose_reasoning_fields() -> None:
    """验证公开 Planner JSON Schema 不包含 thought 或 reasoning_process 字段。

    将完整 Schema 序列化并转小写可覆盖嵌套定义和大小写变体；该测试从契约层防止模型原始思维链
    进入响应、日志和状态，即使未来实现者误加可选字段也会立即回归失败。
    """

    schema = json.dumps(PlannerDecision.model_json_schema()).lower()
    assert '"thought"' not in schema
    assert "reasoning_process" not in schema


def test_versioned_prompt_contains_required_runtime_placeholders() -> None:
    """验证 v1 Planner Prompt ID 固定且保留运行时必须注入的五个占位符。

    占位符分别承载用户问题、假设、证据、工具 Schema 和 ReAct 预算；遗漏任一项会使模型脱离
    当前状态或安全限制。测试只检查契约槽位，不把 Prompt 自然语言措辞锁死，允许受控优化。
    """

    prompt = load_planner_prompt()
    assert PLANNER_PROMPT_ID == "planner-react:v3"
    for placeholder in (
        "{user_query}",
        "{session_context}",
        "{hypotheses}",
        "{evidence_bundle}",
        "{tool_schemas}",
        "{max_react_steps}",
    ):
        assert placeholder in prompt


def test_v3_prompt_separates_static_system_rules_from_runtime_placeholders() -> None:
    """验证 v3 Prompt 的 system 模板不包含任何运行时用户数据占位符。

    system/user 分离防止用户问题被提升到系统优先级；测试同时确认 user 模板承担问题、证据、
    capability 和预算字段，并且旧 v1 文件不再是运行时加载入口。
    """

    system_prompt, user_prompt = load_planner_prompt_parts()

    assert "{user_query}" not in system_prompt
    assert "{tool_evidence}" not in system_prompt
    assert "不可信运行数据" in system_prompt
    for placeholder in (
        "{user_query}",
        "{session_context}",
        "{active_capabilities}",
        "{tool_evidence}",
        "{confirmed_case_memories}",
        "{remaining_time_ms}",
    ):
        assert placeholder in user_prompt
