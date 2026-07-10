"""验证 MCP 服务端 Fixture 仓储的确定性查找和错误标准化。

成功场景必须返回指定工具/资源的固定观察；未知 scenario_id 与缺失结果分别映射到
INVALID_REQUEST 和 EMPTY_RESULT，不能把内部 KeyError 泄露给客户端。
"""

from pathlib import Path

from app.core.fixture_registry import FixtureRegistry
from app.domain.tooling import McpToolRequest, ToolErrorCode, ToolName
from mcp_server.repository import FixtureToolRepository


def _request(scenario_id: str, resource_id: str) -> McpToolRequest:
    """构造带固定时区窗口和 trace 的统一仓储请求，突出场景/资源查找变量。

    辅助函数通过生产 Pydantic 模型校验测试输入，避免仓储测试使用不可能来自 MCP 边界的松散数据；
    固定 trace 不影响精确匹配，因为仓储键只由场景、工具和资源组成。
    """

    return McpToolRequest.model_validate(
        {
            "resource_id": resource_id,
            "time_range": {
                "start": "2026-07-10T00:00:00+08:00",
                "end": "2026-07-10T03:00:00+08:00",
            },
            "scenario_id": scenario_id,
            "trace_id": "trace_repository_001",
        }
    )


def test_repository_returns_scenario_driven_success() -> None:
    """验证仓储对已知场景、工具和资源返回对应的确定性成功响应。

    状态值和首条 source_id 同时断言业务数据与证据来源来自指定 Fixture，而不是默认响应；该测试
    也覆盖注册表加载和深拷贝返回的基本成功路径。
    """

    repository = FixtureToolRepository(
        FixtureRegistry.from_directory(Path("data/fixtures/scenarios"))
    )
    response = repository.execute(
        ToolName.LTS_GET_TASK_STATUS,
        _request("cross_chain_pk_conflict", "dws_order_report_daily"),
    )

    assert response.ok is True
    assert response.data["status"] == "failed"
    assert response.evidence[0].source_id == "lts_status_dws_order_report_daily"


def test_repository_standardizes_unknown_scenario() -> None:
    """验证未知 scenario_id 被转换为 INVALID_REQUEST，而不是泄露内部 KeyError。

    请求本身格式合法但注册表无对应场景，因此失败属于调用输入边界；统一错误响应让 MCP 客户端
    无需理解服务端字典实现，也不会错误重试一个永远不存在的合成场景。
    """

    repository = FixtureToolRepository(
        FixtureRegistry.from_directory(Path("data/fixtures/scenarios"))
    )
    response = repository.execute(
        ToolName.LTS_GET_TASK_STATUS,
        _request("unknown_scenario", "dws_order_report_daily"),
    )

    assert response.ok is False
    assert response.error_code is ToolErrorCode.INVALID_REQUEST


def test_repository_standardizes_missing_tool_result() -> None:
    """验证场景存在但工具/资源组合缺失时返回 EMPTY_RESULT。

    该情况与未知场景区分开：场景 ID 合法，只是没有请求资源的观察。断言标准错误码可保护执行器
    不重试无信息增益的查询，并保证仓储不会选择近似资源或伪造空成功数据。
    """

    repository = FixtureToolRepository(
        FixtureRegistry.from_directory(Path("data/fixtures/scenarios"))
    )
    response = repository.execute(
        ToolName.LTS_GET_TASK_STATUS,
        _request("cross_chain_pk_conflict", "unknown_task"),
    )

    assert response.ok is False
    assert response.error_code is ToolErrorCode.EMPTY_RESULT
