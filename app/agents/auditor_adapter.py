"""组合 Auditor Prompt、结构化 Provider 和最多一次 Schema 修复。

适配器只返回 AuditResult，不执行报告修订。第一次 JSON/Pydantic 失败时在内存中回放截断输出；
拒绝和传输错误直接传播，第二次格式失败由 LangGraph 转为安全降级。
"""

from __future__ import annotations

from app.agents.auditor import (
    AuditorOutputValidationError,
    AuditorTurnContext,
)
from app.agents.auditor_chat import AuditorChatProvider
from app.agents.auditor_prompting import AuditorPromptRenderer
from app.agents.chat import ChatMessage, ChatRole
from app.domain.models import AuditResult


class OpenAICompatibleAuditorAgent:
    """实现 AuditorAgent 协议并限制 Structured Output 修复为零或一次。

    Provider 可替换为真实 SDK 或测试替身；Renderer 固定 Prompt 版本。适配器不捕获 refusal、网络
    错误或模型 revise，也不把无效输出写入 AgentState、事件或日志。
    """

    def __init__(
        self,
        *,
        provider: AuditorChatProvider,
        renderer: AuditorPromptRenderer | None = None,
        repair_count: int = 1,
    ) -> None:
        """注入 Provider/Renderer 并校验 Schema 修复预算只能是零或一。

        构造期读取和审计模板但不发网络请求；非法修复次数显式失败，避免 Auditor 通过无限重试
        消耗预算或规避安全拒绝。Provider 的连接生命周期由运行时工厂管理。
        """

        if repair_count not in {0, 1}:
            raise ValueError("Auditor schema repair_count must be 0 or 1")
        self._provider = provider
        self._renderer = renderer or AuditorPromptRenderer()
        self._repair_count = repair_count

    async def review(self, context: AuditorTurnContext) -> AuditResult:
        """渲染上下文并调用 Provider，首次 Schema 失败后最多修复一次。

        初始消息固定 system/user；修复追加 assistant 原输出和 user 校验摘要，保持报告/证据不变。
        第二次失败抛 attempts=2，任何无效 accept/revise 都不能驱动工作流条件边。
        """

        prompt = self._renderer.render(context)
        messages = (
            ChatMessage(role=ChatRole.SYSTEM, content=prompt.system_message),
            ChatMessage(role=ChatRole.USER, content=prompt.user_message),
        )
        first_failure: AuditorOutputValidationError | None = None
        try:
            return await self._provider.complete(messages)
        except AuditorOutputValidationError as exc:
            if self._repair_count == 0:
                raise
            # except 变量会在块结束后由 Python 清理，因此显式保存供唯一修复调用使用。
            first_failure = exc

        if first_failure is None:
            raise RuntimeError("Auditor repair path requires a validation failure")
        # 原始无效输出只在本次内存调用中回放，不进入 AgentState、事件或持久化日志。
        repair_messages = (
            *messages,
            ChatMessage(
                role=ChatRole.ASSISTANT,
                content=first_failure.raw_output or "{}",
            ),
            ChatMessage(
                role=ChatRole.USER,
                content=_repair_instruction(first_failure.validation_summary),
            ),
        )
        try:
            return await self._provider.complete(repair_messages)
        except AuditorOutputValidationError as second_error:
            raise AuditorOutputValidationError(
                validation_summary=second_error.validation_summary,
                raw_output=second_error.raw_output,
                attempts=2,
            ) from second_error


def _repair_instruction(validation_summary: str) -> str:
    """生成只允许修复 AuditResult JSON/Schema 的第二轮指令。

    指令禁止改变审计事实、增加根因或输出 Thought；完整 Schema 仍由 SDK response_format 提交。
    validation_summary 已由 Pydantic 路径和消息构成并截断，不包含完整响应体或凭据。
    """

    return (
        "上一次 AuditResult 未通过结构化校验。请只修复 JSON 结构和字段组合，"
        "不要增加事实、根因、Observation、Markdown、解释或 Thought。\n"
        f"校验错误：{validation_summary}\n"
        "重新返回一个完整且符合 response_format Schema 的 JSON 对象。"
    )
