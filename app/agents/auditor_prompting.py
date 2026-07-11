"""把 AuditorTurnContext 确定性渲染为版本化 system/user 消息。

渲染器只做 Pydantic 到排序 UTF-8 JSON 的转换，不调用模型。所有运行数据进入 user 消息，system
模板保持静态；AuditResult Schema 由 SDK response_format 单独提交，避免 Prompt/类型双份漂移。
"""

from __future__ import annotations

import json
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field

from app.agents.auditor import AuditorTurnContext
from app.agents.prompts import AUDITOR_PROMPT_ID, load_auditor_prompt_parts

_AUDITOR_USER_FIELDS = frozenset(
    {
        "user_query",
        "draft_report",
        "realtime_evidence",
        "graph_bundle",
        "confirmed_cases",
        "capability_rules",
        "deterministic_issues",
        "revision_number",
    }
)


class AuditorPromptBundle(BaseModel):
    """保存一次可追溯 Auditor 调用的 Prompt ID 和两条独立消息。

    模型冻结并禁止额外字段；system/user 分离保留消息优先级，字段长度限制异常上下文。对象只含
    可公开状态，不包含 API key、SDK 响应或原始 Thought。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(pattern=r"^auditor-report:v\d+$")
    system_message: str = Field(min_length=100, max_length=20_000)
    user_message: str = Field(min_length=100, max_length=250_000)


class AuditorPromptRenderer:
    """加载并审计 v1 Auditor 模板，再把强类型上下文渲染为稳定消息。

    构造时要求 system 无占位符、user 占位符与代码集合完全一致；任何模板漂移在创建 Provider 前
    失败。render 不读取文件外数据，也不接受调用方追加隐藏规则。
    """

    def __init__(self) -> None:
        """读取两份模板并验证动态字段只存在于 user 消息。

        Formatter 正确理解转义花括号，优于正则；字段缺失或多余都抛 ValueError，防止 Prompt 更新
        后静默遗漏 Evidence 或确定性问题。构造不发起任何网络请求。
        """

        self._system_template, self._user_template = load_auditor_prompt_parts()
        system_fields = _template_fields(self._system_template)
        user_fields = _template_fields(self._user_template)
        if system_fields:
            raise ValueError("Auditor system template must not contain runtime placeholders")
        if user_fields != _AUDITOR_USER_FIELDS:
            raise ValueError(
                "Auditor user template placeholders do not match renderer contract: "
                f"expected={sorted(_AUDITOR_USER_FIELDS)}, actual={sorted(user_fields)}"
            )

    def render(self, context: AuditorTurnContext) -> AuditorPromptBundle:
        """序列化报告、证据、规则和审计轮次，返回两条可发送消息。

        Evidence/ToolEvent、GraphRAG 和案例分别保留，防止模型把历史来源伪装成实时 Observation；
        capability 只投影输出校验规则。None Bundle 显式为 null，空问题为 []，不伪造已执行检索。
        """

        state = context.state
        if state.draft_report is None:
            raise ValueError("Auditor renderer requires a draft report")
        # 实时 Evidence/ToolEvent 与图/案例分区序列化，避免来源优先级在一段文本中丢失。
        realtime_evidence = {
            "evidence": [item.model_dump(mode="json") for item in state.evidence],
            "tool_events": [item.model_dump(mode="json") for item in state.tool_events],
            "retrieved_paths": [item.model_dump(mode="json") for item in state.retrieved_paths],
        }
        capability_rules = {
            "active_capabilities": [
                item.value for item in context.capabilities.active_capabilities
            ],
            "output_validation_rules": list(context.capabilities.output_validation_rules),
        }
        # 所有动态值在 format 前先变成规范 JSON，用户花括号不能被二次解释为模板占位符。
        values = {
            "user_query": _json_text(state.user_query),
            "draft_report": _json_text(state.draft_report.model_dump(mode="json")),
            "realtime_evidence": _json_text(realtime_evidence),
            "graph_bundle": _json_text(
                context.evidence_bundle.model_dump(mode="json")
                if context.evidence_bundle is not None
                else None
            ),
            "confirmed_cases": _json_text(
                [item.model_dump(mode="json") for item in context.confirmed_case_memories]
            ),
            "capability_rules": _json_text(capability_rules),
            "deterministic_issues": _json_text(
                [item.model_dump(mode="json") for item in context.deterministic_issues]
            ),
            "revision_number": str(context.revision_number),
        }
        return AuditorPromptBundle(
            prompt_id=AUDITOR_PROMPT_ID,
            system_message=self._system_template.strip(),
            user_message=self._user_template.format_map(values).strip(),
        )


def _template_fields(template: str) -> frozenset[str]:
    """解析模板占位符名称并返回不可变集合，供构造期契约审计。

    只解析原始模板，不解析运行时 JSON 中的花括号；模板语法错误由 Formatter 原样抛出。空模板
    返回空集合，随后由消息长度或字段集合检查失败。
    """

    return frozenset(
        field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None
    )


def _json_text(value) -> str:
    """把 JSON-compatible 数据编码为排序、缩进且保留中文的确定性文本。

    排序键保证相同审计上下文可重放，`ensure_ascii=False` 保持作品演示可读性；未知对象会显式
    抛 TypeError，调用方必须先通过 Pydantic `model_dump(mode="json")` 转换。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    )
