"""MCP 服务端的 scenario_id Fixture 仓储。

只有本模块允许从注册表读取合成响应。未知场景和缺失工具结果被转换为统一错误响应，
因此客户端始终接收相同契约，而不会看到文件路径或 Python KeyError。
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from app.core.fixture_registry import FixtureRegistry
from app.core.settings import get_settings
from app.domain.tooling import (
    McpToolRequest,
    McpToolResponse,
    ToolErrorCode,
    ToolName,
)


class FixtureToolRepository:
    """在 MCP 服务端按场景、工具和资源精确查找已校验合成响应。

    仓储是唯一允许工具处理器接触 FixtureRegistry 的边界；它不读取生产系统，也不推断缺失结果。
    所有未命中都转换成统一失败响应，让客户端始终处理同一 Pydantic 契约。
    """

    def __init__(self, registry: FixtureRegistry) -> None:
        """注入启动阶段已校验的 Fixture 注册表，不在构造时重复执行磁盘 I/O。

        依赖注入允许单元测试使用最小注册表覆盖成功和失败路径，也保证仓储只消费领域对象；注册表
        的非空与唯一性不变量已由其自身构造器负责。
        """

        self._registry = registry

    def execute(self, tool_name: ToolName, request: McpToolRequest) -> McpToolResponse:
        """按 `scenario_id + tool_name + resource_id` 返回响应的深拷贝或标准错误。

        先解析场景，再线性查找该小型演示 Fixture 中唯一工具/资源组合。深拷贝防止某次调用方
        修改响应对象后污染后续可重放结果；未知场景归类 INVALID_REQUEST，场景存在但组合缺失
        归类 EMPTY_RESULT，两者都不包含伪证据且不会抛出文件路径等内部细节。
        """

        try:
            scenario = self._registry.get(request.scenario_id)
        except KeyError:
            # 场景 ID 属于请求契约的一部分，未知值是调用输入错误而非服务暂时不可用。
            return _error_response(
                ToolErrorCode.INVALID_REQUEST,
                f"unknown synthetic scenario: {request.scenario_id}",
            )

        # Fixture 规模刻意保持很小，线性扫描更直观；唯一性已由 ScenarioFixture 校验保证。
        for result in scenario.tool_results:
            if result.tool_name == tool_name and result.request.resource_id == request.resource_id:
                # 返回深拷贝隔离调用间状态，维持相同输入得到相同原始 Fixture 的确定性。
                return result.response.model_copy(deep=True)

        return _error_response(
            ToolErrorCode.EMPTY_RESULT,
            (
                "no synthetic result matched tool "
                f"{tool_name.value} and resource {request.resource_id}"
            ),
        )


def _error_response(error_code: ToolErrorCode, message: str) -> McpToolResponse:
    """构造不含数据和证据、带当前 UTC 观察时间的统一失败响应。

    所有仓储失败都通过该函数满足 `McpToolResponse` 的互斥字段约束，避免不同工具遗漏错误消息或
    意外附带 evidence。调用时间而非 Fixture 时间用于表示“本次查找失败”的实际观察时刻。
    """

    return McpToolResponse(
        ok=False,
        data={},
        evidence=[],
        error_code=error_code,
        error_message=message,
        observed_at=datetime.now(UTC),
    )


@lru_cache
def get_fixture_tool_repository() -> FixtureToolRepository:
    """按进程懒加载并缓存 MCP 服务端 Fixture 仓储。

    首次工具调用从集中配置读取目录、解析全部 JSON 并完成 Schema 校验；后续调用复用内存对象，
    保证低开销和一致视图。加载失败直接传播并使服务调用失败，不会用空仓储伪装可用。
    测试切换环境时应清理该函数与 `get_settings` 的缓存。
    """

    settings = get_settings()
    # 磁盘读取只发生一次，工具热路径随后只执行内存中的确定性精确匹配。
    registry = FixtureRegistry.from_directory(settings.fixture_directory)
    return FixtureToolRepository(registry)
