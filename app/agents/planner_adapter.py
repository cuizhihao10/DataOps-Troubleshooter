"""把版本化 Planner Prompt、结构化 Chat Provider 与一次受控修复组合成 PlannerAgent。

适配器只生成 PlannerDecision，不执行工具。首次 Schema 失败时回放截断输出和校验摘要一次；
refusal、传输错误或第二次失败直接传播，让 LangGraph 写入公开停止原因。
"""

from __future__ import annotations

from app.agents.chat import ChatMessage, ChatRole, PlannerChatProvider
from app.agents.planner import (
    PlannerOutputValidationError,
    PlannerTurnContext,
)
from app.agents.prompting import PlannerPromptRenderer
from app.domain.planner import PlannerDecision


class OpenAICompatiblePlannerAgent:
    """实现 PlannerAgent 协议并限制结构化输出修复最多一次。

    Provider 可替换为真实 OpenAI-compatible SDK 实现或测试替身；Renderer 保持 Prompt 版本化。
    适配器不捕获拒绝和网络错误，也不把原始无效输出写入状态或日志。
    """

    def __init__(
        self,
        *,
        provider: PlannerChatProvider,
        renderer: PlannerPromptRenderer | None = None,
        repair_count: int = 1,
    ) -> None:
        """注入 Provider、可选 Renderer，并校验修复预算只能为零或一次。

        产品契约不允许无限自修复，构造期拒绝其他数字；缺省 Renderer 读取仓库内 v2 模板，
        构造不会发起模型调用。Provider 生命周期由依赖容器负责。
        """

        if repair_count not in {0, 1}:
            raise ValueError("Planner schema repair_count must be 0 or 1")
        self._provider = provider
        self._renderer = renderer or PlannerPromptRenderer()
        self._repair_count = repair_count

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """渲染当前上下文，调用 Provider，并在首次 Schema 失败后最多修复一次。

        初次消息固定为 system/user；修复时追加 assistant 原输出和 user 校验指令，保持原上下文
        不变。第二次失败重新抛出 attempts=2 的安全异常，LangGraph 不会执行任何未校验 Action。
        """

        prompt = self._renderer.render(context)
        messages = (
            ChatMessage(role=ChatRole.SYSTEM, content=prompt.system_message),
            ChatMessage(role=ChatRole.USER, content=prompt.user_message),
        )
        first_failure: PlannerOutputValidationError | None = None
        try:
            return await self._provider.complete(messages)
        except PlannerOutputValidationError as exc:
            if self._repair_count == 0:
                raise
            # Python 会在 except 结束时清除异常变量，因此显式保存仅供本次修复调用使用。
            first_failure = exc

        if first_failure is None:
            raise RuntimeError("Planner repair path requires a validation failure")

        # 无效输出仅在当前调用内回放，未进入 AgentState、公开事件或日志。
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
        except PlannerOutputValidationError as second_error:
            raise PlannerOutputValidationError(
                validation_summary=second_error.validation_summary,
                raw_output=second_error.raw_output,
                attempts=2,
            ) from second_error


def _repair_instruction(validation_summary: str) -> str:
    """生成只要求修复 JSON/Schema 的最小第二轮 user 指令。

    错误摘要来自 Pydantic 字段路径和消息并已截断；指令明确不得增加解释、Markdown 或 Thought，
    也不得改变事实和编造 Observation。完整输出 Schema仍由 SDK response_format 再次提交。
    """

    return (
        "上一次 PlannerDecision 未通过结构化校验。请只修复 JSON 结构和字段组合，"
        "不要添加新事实、Observation、Markdown、解释或 Thought。\n"
        f"校验错误：{validation_summary}\n"
        "重新返回一个完整且符合 response_format Schema 的 JSON 对象。"
    )
