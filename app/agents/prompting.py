"""将 PlannerTurnContext 确定性渲染为版本化 system/user Prompt 消息。

渲染器只做 Pydantic 数据到规范 JSON 文本的转换，不调用模型。用户输入和运行上下文全部进入
user 消息，system 消息保持静态；Structured Outputs Schema 由 SDK 从 PlannerDecision 提交，
不在 Prompt 中重复消耗 token。
"""

from __future__ import annotations

import json
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field

from app.agents.planner import PlannerTurnContext
from app.agents.prompts import PLANNER_PROMPT_ID, load_planner_prompt_parts
from app.domain.tooling import McpToolRequest
from app.reporting.evidence import collect_reference_sources

_USER_TEMPLATE_FIELDS = frozenset(
    {
        "user_query",
        "session_context",
        "plan",
        "active_capabilities",
        "hypotheses",
        "tool_evidence",
        "evidence_bundle",
        "retrieved_paths",
        "confirmed_case_memories",
        "history_case_matches",
        "tool_schemas",
        "trace_id",
        "citable_refs",
        "unexecuted_priority_tools",
        "react_step",
        "max_react_steps",
        "remaining_tool_calls",
        "max_parallel_actions",
        "remaining_time_ms",
    }
)


class PlannerPromptBundle(BaseModel):
    """保存一次可追溯 Planner 调用的版本、system 消息和 user 消息。

    两条消息分开存储，使 provider 能保持角色优先级；模型被冻结并限制额外字段，避免调用前
    临时追加未经版本控制的隐藏规则。消息只包含可公开状态，不包含 Thought。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(pattern=r"^planner-react:v\d+$")
    system_message: str = Field(min_length=100, max_length=20_000)
    user_message: str = Field(min_length=100, max_length=200_000)


class PlannerPromptRenderer:
    """使用版本化模板和规范 JSON 将强类型 Planner 上下文渲染为两条消息。

    构造时读取并审计模板占位符；`render` 每轮只替换批准字段。模板字段缺失或新增会显式失败，
    防止代码与 Prompt 文件静默漂移，也让 Golden Case 能按 prompt_id 重放。
    """

    def __init__(self) -> None:
        """加载 system/user 模板，并验证 user 模板占位符集合完全匹配契约。

        system 模板不允许任何动态字段，从结构上阻止用户问题进入高优先级消息；user 模板必须
        恰好包含已批准字段。校验失败在 Agent 构造时抛出 ValueError，不等待首次模型调用。
        """

        self._system_template, self._user_template = load_planner_prompt_parts()
        system_fields = _template_fields(self._system_template)
        user_fields = _template_fields(self._user_template)
        if system_fields:
            raise ValueError("Planner system template must not contain runtime placeholders")
        if user_fields != _USER_TEMPLATE_FIELDS:
            raise ValueError(
                "Planner user template placeholders do not match the renderer contract: "
                f"expected={sorted(_USER_TEMPLATE_FIELDS)}, actual={sorted(user_fields)}"
            )

    def render(self, context: PlannerTurnContext) -> PlannerPromptBundle:
        """把当前状态、证据、能力和预算渲染为稳定、可审计的 Prompt 消息。

        Pydantic 模型先以 JSON mode 序列化，再用排序键和 UTF-8 规范文本写入模板；`None` 明确
        渲染为 null，空案例渲染为 []，不伪造 GraphRAG 或长期记忆。返回 bundle 不含 API key。
        """

        state = context.state
        # 工具事件只投影 Planner 判断需要的公开字段，不注入 SDK 对象或底层传输细节。
        tool_context = {
            "evidence": [item.model_dump(mode="json") for item in state.evidence],
            "tool_events": [
                {
                    "event_id": event.event_id,
                    "tool_name": event.tool_name.value,
                    "attempt": event.attempt,
                    "ok": event.response.ok,
                    "data": event.response.data,
                    "error_code": (
                        event.response.error_code.value if event.response.error_code else None
                    ),
                    "error_message": event.response.error_message,
                    "observed_at": event.response.observed_at.isoformat(),
                }
                for event in state.tool_events
            ],
            "observation_refs": state.observation_refs,
        }
        # 九个工具共用 McpToolRequest，仅名称按 capability 裁剪，避免重复九份相同 JSON Schema。
        tool_schemas = {
            "allowed_tool_names": [tool.value for tool in context.capabilities.tool_priority],
            "arguments_schema": McpToolRequest.model_json_schema(),
        }

        # 用户文本使用 JSON 字符串编码，换行和伪造章节标题只会作为 user 消息中的数据出现。
        values = {
            "user_query": _json_text(state.user_query),
            "session_context": _json_text(
                state.session_context.model_dump(mode="json")
                if state.session_context is not None
                else None
            ),
            "plan": _json_text(state.plan),
            "active_capabilities": _json_text(context.capabilities.model_dump(mode="json")),
            "hypotheses": _json_text([item.model_dump(mode="json") for item in state.hypotheses]),
            "tool_evidence": _json_text(tool_context),
            "evidence_bundle": _json_text(
                context.evidence_bundle.model_dump(mode="json")
                if context.evidence_bundle is not None
                else None
            ),
            "retrieved_paths": _json_text(
                [item.model_dump(mode="json") for item in state.retrieved_paths]
            ),
            "confirmed_case_memories": _json_text(
                [item.model_dump(mode="json") for item in context.confirmed_case_memories]
            ),
            "history_case_matches": _json_text(
                [item.model_dump(mode="json") for item in context.history_case_matches]
            ),
            "tool_schemas": _json_text(tool_schemas),
            # trace_id 与可引用 ID 都是控制器门禁的判定输入，因此必须显式渲染而不是留给模型推断：
            # 两者本来只存在于 ReactGraphState 里，模型看不到 run_id，也无法区分 evidence_id 和
            # Evidence Bundle 里的 node_id，于是第一步就会被整批拒绝、白花一次付费调用。
            "trace_id": _json_text(state.run_id),
            # 白名单与报告层共用 `collect_reference_sources`，而不是只列实时 evidence_id：草稿、
            # 策略校验、修订和 Auditor 都接受 Bundle 的 kn_*/path_*/dc_* 与 confirmed 案例 ID，
            # 若 Planner 的白名单更窄，模型引用 Prompt 里明明给出的知识证据反而会被门禁整批拒绝
            # （首次真实模型评测的第三个案例就以 invalid_evidence_reference 终止）。同时公开每个
            # ID 的来源类型：只有实时 Observation 能把假设升为 supported，模型需要据此判断。
            "citable_refs": _json_text(
                [
                    {"id": reference_id, "source": source.value}
                    for reference_id, source in collect_reference_sources(
                        state,
                        context.evidence_bundle,
                        context.confirmed_case_memories,
                    ).items()
                ]
            ),
            # 未执行的优先级工具由渲染层算好：模型能看到已有 Observation，却要自己做集合差集才能
            # 发现"用户问的是上游依赖，而依赖拓扑工具还没跑"。首次真实模型评测里两个案例都在缺少
            # 拓扑/表结构证据的情况下就 evidence_sufficient，随后被 Auditor 判为未回答用户问题。
            "unexecuted_priority_tools": _json_text(
                [
                    tool.value
                    for tool in context.capabilities.tool_priority
                    if tool not in {event.tool_name for event in state.tool_events}
                ]
            ),
            "react_step": str(state.react_step),
            "max_react_steps": str(context.max_react_steps),
            # 剩余步数由渲染层直接算出来交给模型，而不是让模型自己做减法：一批 N 个 Action 会消耗
            # N 个步数，模型如果算错就会提交刚好超预算的批次，而每次被控制器拒绝都白花一次调用。
            "remaining_tool_calls": str(context.max_react_steps - state.react_step),
            "max_parallel_actions": str(context.max_parallel_actions),
            "remaining_time_ms": str(context.remaining_time_ms),
        }
        user_message = self._user_template.format_map(values)
        return PlannerPromptBundle(
            prompt_id=PLANNER_PROMPT_ID,
            system_message=self._system_template.strip(),
            user_message=user_message.strip(),
        )


def _template_fields(template: str) -> frozenset[str]:
    """使用标准 Formatter 解析模板字段名，而不是对花括号做脆弱正则匹配。

    返回所有非空字段的不可变集合；模板语法错误由 Formatter 原样抛出。运行时插入的 JSON 可能
    包含大量花括号，但校验只针对原始模板，因此不会把数据内容误判为新占位符。
    """

    return frozenset(
        field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None
    )


def _json_text(value) -> str:
    """把已校验运行数据编码为排序、缩进且保留中文的确定性 JSON 文本。

    `ensure_ascii=False` 保持面试演示可读性，排序键让同一上下文重放得到稳定 Prompt；函数不接收
    自定义编码器，调用方必须先转成 JSON-compatible 数据，失败时显式抛出 TypeError。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    )
