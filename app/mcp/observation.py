"""MCP 响应到 Evidence 与 ToolEvent 的确定性转换。

证据 ID 和事件 ID 使用输入内容的稳定摘要生成，确保同一调用可重放、可引用。重试产生
的多个 Observation 会合并证据但保留全部事件，避免成功重试掩盖首次失败。
"""

from __future__ import annotations

from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import Evidence, EvidenceSourceType, ToolEvent
from app.domain.planner import ToolAction
from app.domain.tooling import RETRYABLE_TOOL_ERRORS, McpToolResponse


class ToolObservation(BaseModel):
    """封装一次 Action 的终态响应、证据引用和完整尝试事件时间线。

    `response` 表示最后一次尝试，`tool_events` 保留初次及重试，evidence 则按稳定 ID 去重。模型
    是执行器写回 AgentState 前的中间边界，不包含 Planner 推理或自行解释的根因。
    """

    model_config = ConfigDict(extra="forbid")

    response: McpToolResponse
    evidence: list[Evidence] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(min_length=1)
    observation_refs: list[str] = Field(default_factory=list)

    @property
    def tool_event(self) -> ToolEvent:
        """返回最后一次尝试事件，供只关心终态的兼容调用方读取。

        完整审计仍应使用 `tool_events`；该属性不删除前序失败，只提供便捷视图。列表由 Pydantic
        强制至少一项，因此索引末项不会产生空列表错误。
        """

        return self.tool_events[-1]


def normalize_observation(
    *,
    action: ToolAction,
    response: McpToolResponse,
    started_at,
    completed_at,
    attempt: int,
) -> ToolObservation:
    """把单次统一 MCP 响应确定性转换成 Evidence、ToolEvent 和引用列表。

    证据与事件 ID 由 trace、工具、来源和 attempt 计算稳定摘要，便于重放引用；只有服务端明确
    返回的 evidence 才会转换，失败空载荷不会生成事实。工具元数据附加到每条证据，事件同时
    保存原请求/响应和时间，使 Auditor 可从结论追溯到协议调用。
    """

    tool_slug = action.tool_name.value.replace(".", "_")

    # 证据 ID 不包含可变自然语言内容，避免措辞微调破坏同一来源在报告中的稳定引用。
    evidence = [
        Evidence(
            evidence_id=_stable_id(
                "ev",
                action.arguments.trace_id,
                action.tool_name.value,
                item.source_id,
            ),
            source_type=EvidenceSourceType.TOOL,
            source_id=item.source_id,
            content=item.content,
            observed_at=response.observed_at,
            reliability=0.95 if response.ok else 0.3,
            metadata={
                **item.metadata,
                "tool_name": action.tool_name.value,
                "trace_id": action.arguments.trace_id,
            },
        )
        for item in response.evidence
    ]
    # attempt 进入事件 ID，使一次重试的两个事件各自可寻址，同时共享同一 trace。
    event = ToolEvent(
        event_id=_stable_id(
            "evt",
            action.arguments.trace_id,
            tool_slug,
            str(attempt),
        ),
        trace_id=action.arguments.trace_id,
        tool_name=action.tool_name,
        request=action.arguments,
        response=response,
        attempt=attempt,
        retryable=response.error_code in RETRYABLE_TOOL_ERRORS,
        started_at=started_at,
        completed_at=completed_at,
    )
    # observation_refs 只引用实际创建的证据；失败响应因此自然得到空引用列表。
    return ToolObservation(
        response=response,
        evidence=evidence,
        tool_events=[event],
        observation_refs=[item.evidence_id for item in evidence],
    )


def merge_observations(observations: list[ToolObservation]) -> ToolObservation:
    """合并初次与重试 Observation，保留终态响应、全部事件和唯一证据。

    输入顺序就是尝试顺序，因此最后响应代表执行器终态；证据按稳定 ID 去重，事件不去重以保存
    每次真实调用。空输入代表执行器控制流错误，显式抛出 ValueError 而不是构造无事件观察。
    """

    if not observations:
        raise ValueError("at least one tool observation is required")

    # 字典保持首次插入顺序；相同来源重放不会重复污染证据集合。
    evidence_by_id = {
        item.evidence_id: item for observation in observations for item in observation.evidence
    }
    # 事件采用扁平列表完整串联，终态 response 则只取最后一次尝试，二者语义刻意不同。
    return ToolObservation(
        response=observations[-1].response,
        evidence=list(evidence_by_id.values()),
        tool_events=[event for observation in observations for event in observation.tool_events],
        observation_refs=list(evidence_by_id),
    )


def _stable_id(prefix: str, *parts: str) -> str:
    """用规范部件生成短而稳定的 SHA-256 引用 ID。

    分隔符避免简单拼接歧义，SHA-256 截断为 16 个十六进制字符以兼顾可读性和演示规模下的
    冲突概率；前缀区分 Evidence 与 Event 命名空间。本函数不用于安全令牌或凭据生成。
    """

    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
