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
    tool_event: ToolEvent
    observation_refs: list[str] = Field(default_factory=list)


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
        tool_event=event,
        observation_refs=[item.evidence_id for item in evidence],
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
