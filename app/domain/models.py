from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.planner import PlannerDecision
from app.domain.tooling import McpToolRequest, McpToolResponse, ToolName


class Component(StrEnum):
    LTS = "lts"
    BDS = "bds"
    FLASHSYNC = "flashsync"


class EvidenceSourceType(StrEnum):
    TOOL = "tool"
    KNOWLEDGE_NODE = "knowledge_node"
    GRAPH_PATH = "graph_path"
    CASE_MEMORY = "case_memory"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=100)
    source_type: EvidenceSourceType
    source_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    observed_at: datetime
    reliability: float = Field(ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HypothesisStatus(StrEnum):
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class FaultHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=100)
    symptom: str = Field(min_length=1, max_length=1000)
    candidate_root_cause: str = Field(min_length=1, max_length=1000)
    components: list[Component] = Field(min_length=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.CANDIDATE
    confidence: float = Field(default=0, ge=0, le=1)


class RootCauseConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(min_length=1)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RemediationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=2000)
    risk_level: RiskLevel
    prerequisites: list[str] = Field(default_factory=list)
    rollback: str = Field(min_length=1, max_length=2000)
    verification: str = Field(min_length=1, max_length=2000)


class SimilarCaseReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=100)
    similarity: float = Field(ge=0, le=1)
    confirmed: bool
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4000)
    fault_chain: list[str] = Field(default_factory=list)
    root_causes: list[RootCauseConclusion] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    remediation_steps: list[RemediationStep] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    similar_cases: list[SimilarCaseReference] = Field(default_factory=list)


class MemoryStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class CaseMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=100)
    symptoms: list[str] = Field(min_length=1)
    root_cause: str = Field(min_length=1, max_length=1000)
    fault_path: list[str] = Field(default_factory=list)
    solution_steps: list[str] = Field(default_factory=list)
    components: list[Component] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(min_length=1)
    status: MemoryStatus = MemoryStatus.PENDING
    occurrence_count: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class ToolEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=100)
    trace_id: str = Field(min_length=3, max_length=100)
    tool_name: ToolName
    request: McpToolRequest
    response: McpToolResponse
    attempt: int = Field(default=1, ge=1, le=2)
    retryable: bool = False
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_timing(self) -> ToolEvent:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("tool event timestamps must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        return self


class RetrievedPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path_id: str = Field(min_length=1, max_length=100)
    node_ids: list[str] = Field(min_length=2)
    relation_types: list[str] = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    source_ids: list[str] = Field(min_length=1)


class AuditStatus(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"


class AuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AuditStatus
    issues: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=100)
    user_query: str = Field(min_length=1, max_length=4000)
    intent: str | None = Field(default=None, max_length=100)
    active_capabilities: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    hypotheses: list[FaultHypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    retrieved_paths: list[RetrievedPath] = Field(default_factory=list)
    react_step: int = Field(default=0, ge=0)
    next_action: PlannerDecision | None = None
    observation_refs: list[str] = Field(default_factory=list)
    stop_reason: str | None = Field(default=None, max_length=500)
    draft_report: DiagnosisReport | None = None
    audit_result: AuditResult | None = None
    retry_count: int = Field(default=0, ge=0, le=1)
    memory_candidate: CaseMemory | None = None
