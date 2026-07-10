"""九个 MCP 工具的白名单、统一输入输出和错误分类。

所有组件共享同一请求/响应外壳，使 Planner 只处理标准 Observation。跨字段校验拒绝
成功响应携带错误或失败响应缺少错误信息，并明确哪些错误允许执行一次瞬时重试。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolName(StrEnum):
    """列出产品设计批准的九个只读 MCP 工具完整名称。

    枚举同时约束 Planner Action、Fixture、MCP 服务注册和集成测试，防止任一层静默改名、合并
    或新增未审计工具；字符串值与协议层公开名称保持完全一致。
    """

    LTS_GET_TASK_STATUS = "lts.get_task_status"
    LTS_GET_TASK_LOG = "lts.get_task_log"
    LTS_GET_DEPENDENCY_TOPOLOGY = "lts.get_dependency_topology"
    BDS_GET_TASK_STATUS = "bds.get_task_status"
    BDS_GET_TASK_LOG = "bds.get_task_log"
    BDS_GET_TABLE_INFO = "bds.get_table_info"
    FLASHSYNC_GET_SYNC_DELAY = "flashsync.get_sync_delay"
    FLASHSYNC_GET_SYNC_LOG = "flashsync.get_sync_log"
    FLASHSYNC_CHECK_CONSISTENCY = "flashsync.check_consistency"


class ToolErrorCode(StrEnum):
    """统一跨 LTS、BDS 和 FlashSync 的工具失败分类。

    错误类别决定是否值得重试及报告如何降级；只有超时和暂时不可用属于瞬时错误，权限拒绝、
    空结果和非法请求不会因重复调用而增加信息。
    """

    INVALID_REQUEST = "INVALID_REQUEST"
    EMPTY_RESULT = "EMPTY_RESULT"
    TIMEOUT = "TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class TimeRange(BaseModel):
    """表示工具查询使用的带时区半开放式时间上下界。

    模型要求结束时间严格晚于开始时间并强制时区，避免跨容器或夏令时环境解释不一致；具体工具
    可按自身语义读取区间，但不能接收倒置或无时区时间。
    """

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_range(self) -> TimeRange:
        """拒绝无时区或非递增的工具查询时间范围。

        先检查时区是因为两个 naive datetime 虽可比较，却无法映射到统一观察时间线；随后要求
        end 大于 start，避免零长度或倒置查询在下游产生含糊空结果。
        """

        # 时区完整性属于可审计性约束，应在任何大小比较前明确验证。
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("time_range values must include a timezone")
        if self.end <= self.start:
            raise ValueError("time_range.end must be later than time_range.start")
        return self


class McpToolRequest(BaseModel):
    """定义九个 MCP 工具共享的最小、可追踪请求外壳。

    资源 ID 指定调查对象，时间范围限制观察窗口，scenario_id 选择合成数据，trace_id 串联一次
    诊断。共享 Schema 使 Planner 和执行器无需为每个组件维护不同参数解析逻辑。
    """

    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(min_length=1, max_length=200)
    time_range: TimeRange
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    trace_id: str = Field(min_length=3, max_length=100)


class ToolEvidencePayload(BaseModel):
    """表示 MCP 服务返回、尚待标准化为领域 Evidence 的证据载荷。

    服务端只提供来源 ID、可引用内容和结构化元数据，不自行生成最终 evidence_id 或可靠性；
    这些审计属性由客户端 Observation 适配器按确定性规则补齐。
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class McpToolResponse(BaseModel):
    """统一表示 MCP 工具的成功数据、证据或结构化失败。

    成功与错误字段严格互斥，所有响应必须包含带时区观察时间。这样执行器可以只依据 `ok` 和
    error_code 做重试决策，并保证失败响应不会携带伪造 Evidence 混入诊断状态。
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[ToolEvidencePayload] = Field(default_factory=list)
    error_code: ToolErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=1000)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_success_or_error(self) -> McpToolResponse:
        """校验观察时间和成功/失败字段组合的一致性。

        成功响应不得残留错误，失败响应必须同时给出机器码和可读消息；校验在协议载荷进入领域层
        时执行，任何矛盾返回都会抛出 ValidationError 而不是让调用方猜测优先级。
        """

        # 所有证据排序都依赖绝对时间，因此即使 Mock 数据也必须提供时区。
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        # `ok` 是唯一分支开关，错误字段必须与它保持严格互斥才能安全自动化。
        if self.ok and (self.error_code is not None or self.error_message is not None):
            raise ValueError("successful responses cannot include error fields")
        if not self.ok and (self.error_code is None or not self.error_message):
            raise ValueError("failed responses require error_code and error_message")
        return self


RETRYABLE_TOOL_ERRORS: frozenset[ToolErrorCode] = frozenset(
    {ToolErrorCode.TIMEOUT, ToolErrorCode.SERVICE_UNAVAILABLE}
)
