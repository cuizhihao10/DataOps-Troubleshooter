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
from app.domain.planner import (
    MAX_PARALLEL_TOOL_ACTIONS,
    HypothesisUpdate,
    PlannerDecision,
    PlannerStopReason,
)

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
    """验证合法 call_tool 决策必须携带至少一个可解析的白名单 ToolAction。

    测试从字典经过完整 PlannerDecision Schema，断言嵌套 Action 和工具枚举被正确构造；这保护
    ReAct 工作流只能执行结构化参数，不能从 decision_summary 自然语言猜测外部动作。
    """

    decision = PlannerDecision.model_validate(
        {
            "status": "call_tool",
            "decision_summary": "先确认 LTS 当前状态。",
            "hypothesis_updates": [],
            "actions": [VALID_ACTION],
            "evidence_refs": [],
            "stop_reason": None,
        }
    )
    assert len(decision.actions) == 1
    assert decision.actions[0].tool_name.value == "lts.get_task_status"


def test_call_tool_decision_requires_a_non_empty_action_batch() -> None:
    """验证 call_tool 状态不能提交空 actions 数组来伪装"已决定行动"。

    空批次会让路由进入执行节点却没有任何工具可跑，从而在图里表现为一次无声空转；把它挡在
    Schema 层比在节点里补 RuntimeError 更早，也让替身 Planner 无法制造这种状态。
    """

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": "call_tool",
                "decision_summary": "准备调用工具。",
                "hypothesis_updates": [],
                "actions": [],
                "evidence_refs": [],
                "stop_reason": None,
            }
        )


def test_call_tool_decision_rejects_batch_over_parallel_cap() -> None:
    """验证超过 MAX_PARALLEL_TOOL_ACTIONS 的批次在 Schema 层就被拒绝。

    上限写在校验器而不是 `Field(max_length=...)`，因为 Structured Outputs 的 strict Schema 不接受
    `maxItems`；这项断言保证"换成校验器实现"没有把上限本身弄丢，四个 Action 必须失败。
    """

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": "call_tool",
                "decision_summary": "一次查完所有工具。",
                "hypothesis_updates": [],
                "actions": [VALID_ACTION] * (MAX_PARALLEL_TOOL_ACTIONS + 1),
                "evidence_refs": [],
                "stop_reason": None,
            }
        )


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
                "actions": [],
                "evidence_refs": [],
                "stop_reason": None,
            }
        )


def test_non_call_tool_decision_rejects_action() -> None:
    """验证结束状态即使证据充分也不能夹带待执行 ToolAction。

    构造 finish 与合法 Action 的矛盾组合并期望校验失败，防止工作流在记录“已结束”的同时产生
    未审计副作用；一次结构化决策必须只能选择继续调用或停止其中一种控制流。
    """

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": "finish",
                "decision_summary": "证据充分。",
                "hypothesis_updates": [],
                "actions": [VALID_ACTION],
                "evidence_refs": ["ev_001"],
                "stop_reason": "evidence_sufficient",
            }
        )


def test_stop_reason_must_be_an_evaluable_enum_value_not_free_text() -> None:
    """验证停止原因只接受七个枚举值，一段自然语言理由必须被契约拒绝。

    停止原因会进入 `run_events`、`run-trace:v1` span 属性和 Golden 评测的分类比较：自由文本会同时
    造成公开事件出现接近思维链的长篇叙述、span 属性白名单拒绝非 ASCII 值、以及分类命中恒为假。
    首次真实模型冒烟评测的 `stop_reason_hit_rate` 实测为 0 就是因为模型给出的是整段中文理由。
    """

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "status": "finish",
                "decision_summary": "证据充分。",
                "hypothesis_updates": [],
                "actions": [],
                "evidence_refs": [],
                "stop_reason": "已经确认根因是分区日期格式错误，建议人工修正参数后重跑。",
            }
        )

    decision = PlannerDecision.model_validate(
        {
            "status": "finish",
            "decision_summary": "证据充分。",
            "hypothesis_updates": [],
            "actions": [],
            "evidence_refs": [],
            "stop_reason": "evidence_sufficient",
        }
    )
    assert decision.stop_reason is PlannerStopReason.EVIDENCE_SUFFICIENT
    assert {reason.value for reason in PlannerStopReason} == {
        "evidence_sufficient",
        "evidence_insufficient",
        "evidence_conflict_requires_manual_review",
        "tool_unavailable_degraded",
        "permission_denied_requires_access",
        "missing_resource_id",
        "need_user_input",
    }
    assert all(reason.value.isascii() for reason in PlannerStopReason)


def test_new_hypothesis_update_requires_symptom_and_candidate_root_cause() -> None:
    """验证新建假设必须携带症状与候选根因，其余状态可以只引用既有假设。

    `hypothesis_updates` 是模型结论进入报告根因的唯一通道。若新建允许缺内容，控制器就无法构造
    合法 `FaultHypothesis`，只能静默丢弃，从而重现"模型说出了根因、报告里却没有根因"的结构性
    缺陷——首次真实模型评测三个案例全部 `safe_degraded` 正是这条链路造成的。空白字符串按缺失
    处理，避免用空格绕过校验；strengthened 省略这两个字段合法，因为内容已经存在于状态中。
    """

    with pytest.raises(ValidationError):
        HypothesisUpdate.model_validate(
            {"hypothesis_id": "hyp_001", "status": "new", "evidence_refs": []}
        )
    with pytest.raises(ValidationError):
        HypothesisUpdate.model_validate(
            {
                "hypothesis_id": "hyp_001",
                "status": "new",
                "symptom": "   ",
                "candidate_root_cause": "分区日期格式错误。",
            }
        )
    update = HypothesisUpdate.model_validate(
        {"hypothesis_id": "hyp_001", "status": "strengthened", "evidence_refs": ["ev_001"]}
    )
    assert update.symptom is None
    assert update.candidate_root_cause is None


def test_planner_schema_does_not_expose_reasoning_fields() -> None:
    """验证公开 Planner JSON Schema 不包含 thought 或 reasoning_process 字段。

    将完整 Schema 序列化并转小写可覆盖嵌套定义和大小写变体；该测试从契约层防止模型原始思维链
    进入响应、日志和状态，即使未来实现者误加可选字段也会立即回归失败。
    """

    schema = json.dumps(PlannerDecision.model_json_schema()).lower()
    assert '"thought"' not in schema
    assert "reasoning_process" not in schema


def test_versioned_prompt_contains_required_runtime_placeholders() -> None:
    """验证 Planner Prompt ID 固定且保留运行时必须注入的全部占位符。

    占位符分别承载用户问题、假设、证据、工具 Schema 和 ReAct 预算（含剩余步数与批次上限）；
    遗漏任一项会使模型脱离当前状态或安全限制。测试只检查契约槽位，不把 Prompt 自然语言措辞
    锁死，允许受控优化。
    """

    prompt = load_planner_prompt()
    assert PLANNER_PROMPT_ID == "planner-react:v8"
    for placeholder in (
        "{user_query}",
        "{session_context}",
        "{history_case_matches}",
        "{hypotheses}",
        "{evidence_bundle}",
        "{tool_schemas}",
        "{trace_id}",
        "{citable_refs}",
        "{unexecuted_priority_tools}",
        "{max_react_steps}",
        "{remaining_tool_calls}",
        "{max_parallel_actions}",
    ):
        assert placeholder in prompt


def test_v8_prompt_separates_static_system_rules_from_runtime_placeholders() -> None:
    """验证 v8 Prompt 的 system 模板不包含任何运行时用户数据占位符。

    system/user 分离防止用户问题被提升到系统优先级；测试同时确认 user 模板承担问题、证据、
    capability 和预算字段，并确认批次独立性、trace_id 逐字复制、evidence_refs 白名单、
    hypothesis_updates 是结论唯一通道、stop_reason 七个枚举值，以及 v8 新增的两条口径——只有
    source 为 tool 的实时 Observation 引用能把假设升为 supported、优先级工具未跑完不得直接结束——
    都写在静态 system 侧，而不是可被运行数据改写的位置。
    """

    system_prompt, user_prompt = load_planner_prompt_parts()

    assert "{user_query}" not in system_prompt
    assert "{tool_evidence}" not in system_prompt
    assert "不可信运行数据" in system_prompt
    assert "互不依赖" in system_prompt
    assert "并行只缩短等待时间" in system_prompt
    assert "逐字复制" in system_prompt
    assert "白名单" in system_prompt
    assert "hypothesis_updates" in system_prompt
    assert "decision_summary 只是给人看的说明文字" in system_prompt
    assert "source 为 tool 的实时 Observation 引用" in system_prompt
    assert "不得直接 finish" in system_prompt
    for reason in PlannerStopReason:
        assert reason.value in system_prompt
    for placeholder in (
        "{user_query}",
        "{session_context}",
        "{history_case_matches}",
        "{active_capabilities}",
        "{tool_evidence}",
        "{confirmed_case_memories}",
        "{trace_id}",
        "{citable_refs}",
        "{unexecuted_priority_tools}",
        "{remaining_time_ms}",
        "{max_parallel_actions}",
    ):
        assert placeholder in user_prompt
