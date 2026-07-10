"""将单个 Planner ToolAction 执行为可审计 Observation。

执行器只对 TIMEOUT 和 SERVICE_UNAVAILABLE 进行最多一次重试，并为每次尝试保留独立
ToolEvent。它不解释业务含义，也不生成根因，从而保持工具执行节点的确定性。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.planner import ToolAction
from app.domain.tooling import RETRYABLE_TOOL_ERRORS, McpToolResponse
from app.mcp.client import McpClientError, StdioMcpClient
from app.mcp.observation import (
    ToolObservation,
    merge_observations,
    normalize_observation,
)


class McpToolExecutor:
    """执行单个 Planner ToolAction，并将所有尝试合并成可审计 Observation。

    执行器只负责确定性控制流：调用客户端、按错误码最多重试一次、保留每次 ToolEvent 并合并
    证据。它不判断根因，也不把传输失败伪造成证据，从而让 Planner 只能基于真实 Observation
    更新假设。
    """

    def __init__(self, client: StdioMcpClient, *, retry_count: int) -> None:
        """注入 MCP 客户端并校验重试预算只能为零或一次。

        产品基线限制瞬时重试一次，构造期拒绝更大值可防止配置错误放大工具压力；客户端注入
        便于集成真实 stdio 实现，也便于单元测试使用可控替身验证失败路径。
        """

        if retry_count not in {0, 1}:
            raise ValueError("retry_count must be 0 or 1")
        self._client = client
        self._retry_count = retry_count

    async def execute(self, action: ToolAction) -> ToolObservation:
        """执行 Action，遇到瞬时错误时按预算重试，并返回合并后的终态观察。

        循环次数等于初次尝试加重试预算；每次结果先加入列表，再依据统一错误集合决定是否继续。
        成功响应的 error_code 为 None，因此立即停止；权限拒绝、空结果等非瞬时失败也不会重试。
        合并结果以最后响应为终态，同时完整保留所有事件和去重证据。
        """

        observations: list[ToolObservation] = []
        for attempt in range(1, self._retry_count + 2):
            # 无论成功失败都先记录尝试，确保后续成功不会抹掉首次超时的审计事实。
            observation = await self._execute_once(action, attempt=attempt)
            observations.append(observation)

            # 只有预先批准的瞬时错误值得重复同一只读调用，其余结果直接成为终态。
            if observation.response.error_code not in RETRYABLE_TOOL_ERRORS:
                break
        return merge_observations(observations)

    async def _execute_once(
        self,
        action: ToolAction,
        *,
        attempt: int,
    ) -> ToolObservation:
        """完成一次 MCP 调用并把客户端异常标准化为失败响应和 ToolEvent。

        开始与结束时间在执行器侧采集，用于衡量包含传输开销的真实耗时；客户端若未返回合法响应，
        则只构造 `evidence=[]` 的标准失败对象。最后统一调用 Observation 适配器生成稳定事件 ID，
        因而成功和失败走同一审计路径，且异常不会被吞成空成功。
        """

        started_at = datetime.now(UTC)
        try:
            response = await self._client.call_tool(action.tool_name, action.arguments)
        except McpClientError as exc:
            # 传输层没有可信工具事实，因此失败响应必须保持空 evidence，避免制造伪证据。
            response = McpToolResponse(
                ok=False,
                data={},
                evidence=[],
                error_code=exc.error_code,
                error_message=str(exc)[:1000],
                observed_at=datetime.now(UTC),
            )
        # completed_at 在异常标准化后采集，使事件耗时覆盖错误映射但不包含后续模型处理。
        completed_at = datetime.now(UTC)
        return normalize_observation(
            action=action,
            response=response,
            started_at=started_at,
            completed_at=completed_at,
            attempt=attempt,
        )
