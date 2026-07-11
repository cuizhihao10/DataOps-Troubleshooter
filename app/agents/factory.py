"""把集中 Settings 转换为可关闭的 Planner/Auditor Agent 运行时依赖。

工厂只支持 disabled 与固定 OpenAI-compatible 实现，不建设动态插件系统。两个角色保持独立
Prompt、Schema 和 Provider 资源责任；API lifespan 在退出时分别关闭其自有 SDK 连接池。
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI

from app.agents.auditor_adapter import OpenAICompatibleAuditorAgent
from app.agents.auditor_chat import OpenAICompatibleAuditorProvider
from app.agents.auditor_prompting import AuditorPromptRenderer
from app.agents.chat import OpenAICompatiblePlannerProvider
from app.agents.planner_adapter import OpenAICompatiblePlannerAgent
from app.agents.prompting import PlannerPromptRenderer
from app.core.settings import Settings


@dataclass(frozen=True, slots=True)
class PlannerRuntime:
    """成对保存 Planner Agent 与拥有 HTTP 资源的具体 Provider。

    Agent 只依赖协议而不知道关闭语义，Provider 则持有 AsyncOpenAI 连接池；运行时容器让 FastAPI
    lifespan 能同时复用 Agent 和精确释放资源，不把 SDK 客户端写入 AgentState。
    """

    agent: OpenAICompatiblePlannerAgent
    provider: OpenAICompatiblePlannerProvider

    async def aclose(self) -> None:
        """在应用退出时委托 Provider 关闭其自有异步 HTTP 连接池。

        Provider 会区分自建与注入客户端，因此测试注入的 MockTransport 客户端仍由测试管理；
        本方法不吞关闭异常，资源清理问题应在 lifespan 日志中显式暴露。
        """

        await self.provider.aclose()


@dataclass(frozen=True, slots=True)
class AuditorRuntime:
    """成对保存独立 Auditor Agent 和拥有 HTTP 资源的具体 Provider。

    Auditor 与 Planner 不共享可变 Agent 状态或 Prompt；运行时容器只负责生命周期，让 FastAPI
    可以复用审计 Agent 并在退出时精确关闭连接池，不把 SDK 对象放入 LangGraph checkpoint。
    """

    agent: OpenAICompatibleAuditorAgent
    provider: OpenAICompatibleAuditorProvider

    async def aclose(self) -> None:
        """在应用退出时关闭 Auditor Provider 自行创建的异步 HTTP 连接池。

        注入客户端由测试或外部依赖容器管理，Provider 会跳过关闭；本方法不吞异常，资源清理失败
        因而能够在 lifespan 测试和部署日志中被发现。
        """

        await self.provider.aclose()


def create_planner_runtime(
    settings: Settings,
    *,
    client: AsyncOpenAI | None = None,
) -> PlannerRuntime | None:
    """根据集中配置创建 OpenAI-compatible Planner，disabled 时明确返回 None。

    Settings 已保证启用 Provider 时 API key 存在且 URL 不含用户信息；工厂不发起网络探测，避免
    健康启动产生付费请求。未知 Provider 由 Literal 字段阻止，缺失 key 仍防御性抛出 ValueError。
    """

    if settings.chat_provider == "disabled":
        return None
    if settings.chat_api_key is None:
        raise ValueError("chat_api_key is required when Planner provider is enabled")

    # 先审计本地模板；若占位符漂移，不能先创建一个随后无人关闭的 HTTP 客户端。
    renderer = PlannerPromptRenderer()
    provider = OpenAICompatiblePlannerProvider(
        api_key=settings.chat_api_key,
        base_url=str(settings.chat_base_url),
        model=settings.chat_model,
        timeout_seconds=settings.chat_timeout_seconds,
        client=client,
    )
    agent = OpenAICompatiblePlannerAgent(
        provider=provider,
        renderer=renderer,
        repair_count=settings.planner_schema_repair_count,
    )
    return PlannerRuntime(agent=agent, provider=provider)


def create_auditor_runtime(
    settings: Settings,
    *,
    client: AsyncOpenAI | None = None,
) -> AuditorRuntime | None:
    """根据集中配置创建独立 Auditor，disabled 时明确返回 None。

    Settings 已验证共享端点和 SecretStr key；工厂先审计本地 Prompt，再创建 Provider，避免模板
    漂移后遗留连接池。构造不发送模型探测或付费请求，注入客户端便于真实 SDK MockTransport 测试。
    """

    if settings.chat_provider == "disabled":
        return None
    if settings.chat_api_key is None:
        raise ValueError("chat_api_key is required when Auditor provider is enabled")

    # Prompt 审计是纯本地步骤，必须先于持有网络资源的 Provider 创建。
    renderer = AuditorPromptRenderer()
    provider = OpenAICompatibleAuditorProvider(
        api_key=settings.chat_api_key,
        base_url=str(settings.chat_base_url),
        model=settings.chat_model,
        timeout_seconds=settings.chat_timeout_seconds,
        client=client,
    )
    agent = OpenAICompatibleAuditorAgent(
        provider=provider,
        renderer=renderer,
        repair_count=settings.auditor_schema_repair_count,
    )
    return AuditorRuntime(agent=agent, provider=provider)
