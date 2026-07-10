"""验证九工具白名单、时间范围、统一响应和重试错误分类。

这些测试保护产品文档规定的工具名称不被静默修改，并确保带时区时间、成功/错误字段组合
与瞬时错误集合在 MCP 客户端和服务端之间保持一致。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.tooling import (
    RETRYABLE_TOOL_ERRORS,
    McpToolRequest,
    McpToolResponse,
    TimeRange,
    ToolErrorCode,
    ToolName,
)

EXPECTED_TOOL_NAMES = {
    "lts.get_task_status",
    "lts.get_task_log",
    "lts.get_dependency_topology",
    "bds.get_task_status",
    "bds.get_task_log",
    "bds.get_table_info",
    "flashsync.get_sync_delay",
    "flashsync.get_sync_log",
    "flashsync.check_consistency",
}


def test_tool_allowlist_matches_product_contract() -> None:
    """验证 ToolName 枚举与产品批准的九个公开名称精确相等。

    集合相等同时捕获遗漏、静默改名和未经审批的新增工具；服务注册、Planner Action 与 Fixture
    都依赖该枚举，因此这是协议白名单最靠近产品基线的快速门禁。
    """

    assert {tool.value for tool in ToolName} == EXPECTED_TOOL_NAMES


def test_time_range_requires_timezone_and_increasing_values() -> None:
    """验证工具时间范围拒绝无时区值和结束早于开始的倒置区间。

    两个独立失败用例分别证明绝对时间可审计性与区间顺序约束，防止调用方把含糊本地时间或负窗口
    传入三个组件；两种错误都应在调用 MCP 前由 Pydantic 拦截。
    """

    with pytest.raises(ValidationError):
        TimeRange(
            start=datetime(2026, 7, 10, 1, 0),
            end=datetime(2026, 7, 10, 2, 0),
        )

    with pytest.raises(ValidationError):
        TimeRange(
            start=datetime(2026, 7, 10, 2, 0, tzinfo=UTC),
            end=datetime(2026, 7, 10, 1, 0, tzinfo=UTC),
        )


def test_unknown_tool_name_is_rejected() -> None:
    """验证任意未列入九工具白名单的字符串不能构造 ToolName。

    使用看似合理但未批准的 `lts.fetch_everything` 模拟模型越权扩张；ValueError 证明 Planner 无法
    通过自由文本调用开放式读取接口，从枚举边界维持最小只读工具面。
    """

    with pytest.raises(ValueError):
        ToolName("lts.fetch_everything")


def test_tool_request_rejects_blank_resource_id() -> None:
    """验证统一 MCP 请求在其他字段均合法时仍拒绝空资源标识。

    空 ID 无法精确绑定任务、表或同步对象，若传到 Fixture 仓储只会产生含糊空结果；因此测试要求
    Pydantic 在协议调用前失败，避免 Planner 消耗重试预算调查无目标请求。
    """

    with pytest.raises(ValidationError):
        McpToolRequest.model_validate(
            {
                "resource_id": "",
                "time_range": {
                    "start": "2026-07-10T01:00:00+08:00",
                    "end": "2026-07-10T02:00:00+08:00",
                },
                "scenario_id": "valid_scenario",
                "trace_id": "trace_001",
            }
        )


def test_success_response_cannot_contain_error_fields() -> None:
    """验证 `ok=True` 的响应不能同时携带错误码和错误消息。

    矛盾响应会让执行器无法确定是否重试，也可能把失败证据当成功事实；测试故意组合成功标记与
    TIMEOUT，要求跨字段 validator 拒绝数据，而不是由调用方猜测字段优先级。
    """

    with pytest.raises(ValidationError):
        McpToolResponse.model_validate(
            {
                "ok": True,
                "data": {},
                "evidence": [],
                "error_code": "TIMEOUT",
                "error_message": "unexpected",
                "observed_at": "2026-07-10T02:00:00+08:00",
            }
        )


def test_failed_response_requires_error_code_and_message() -> None:
    """验证 `ok=False` 的响应必须同时提供机器错误码和可读错误消息。

    错误码驱动确定性重试策略，消息支持审计和用户降级说明；缺少二者时失败不可操作。测试要求
    Pydantic 拦截不完整响应，且不允许空 data/evidence 掩盖契约缺失。
    """

    with pytest.raises(ValidationError):
        McpToolResponse.model_validate(
            {
                "ok": False,
                "data": {},
                "evidence": [],
                "observed_at": "2026-07-10T02:00:00+08:00",
            }
        )


def test_only_transient_errors_are_retryable() -> None:
    """验证重试白名单只包含 TIMEOUT 与 SERVICE_UNAVAILABLE，并明确排除权限拒绝。

    精确集合保护产品“一次瞬时重试”策略，防止空结果或非法请求被无效重复；额外权限断言强调
    重试不能绕过访问控制，也不能把持续拒绝放大为对工具服务的压力。
    """

    assert RETRYABLE_TOOL_ERRORS == {
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.SERVICE_UNAVAILABLE,
    }
    assert ToolErrorCode.PERMISSION_DENIED not in RETRYABLE_TOOL_ERRORS
