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
    model_config = ConfigDict(extra="forbid")

    response: McpToolResponse
    evidence: list[Evidence] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(min_length=1)
    observation_refs: list[str] = Field(default_factory=list)

    @property
    def tool_event(self) -> ToolEvent:
        """Return the final attempt event for callers that need the terminal result."""
        return self.tool_events[-1]


def normalize_observation(
    *,
    action: ToolAction,
    response: McpToolResponse,
    started_at,
    completed_at,
    attempt: int,
) -> ToolObservation:
    tool_slug = action.tool_name.value.replace(".", "_")
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
    return ToolObservation(
        response=response,
        evidence=evidence,
        tool_events=[event],
        observation_refs=[item.evidence_id for item in evidence],
    )


def merge_observations(observations: list[ToolObservation]) -> ToolObservation:
    if not observations:
        raise ValueError("at least one tool observation is required")

    evidence_by_id = {
        item.evidence_id: item for observation in observations for item in observation.evidence
    }
    return ToolObservation(
        response=observations[-1].response,
        evidence=list(evidence_by_id.values()),
        tool_events=[event for observation in observations for event in observation.tool_events],
        observation_refs=list(evidence_by_id),
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
