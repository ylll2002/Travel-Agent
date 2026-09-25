from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FlexibleModel(BaseModel):
    """允许 Agent 增加字段，同时为稳定字段提供类型检查。"""

    model_config = ConfigDict(extra="allow")


class TimeCandidate(FlexibleModel):
    label: str
    start_date: str | None = None
    end_date: str | None = None
    duration_days: int | None = None
    reason: str | None = None
    activities: list[str] = Field(default_factory=list)


class EvidenceRef(FlexibleModel):
    evidence_id: str | None = None
    source: str | None = None
    source_type: str | None = None
    url: str | None = None
    fetched_at: str | None = None
    retrieved_at: str | None = None
    status: str | None = None


class ItineraryCard(FlexibleModel):
    card_id: str
    day: int
    type: str
    title: str
    time: str | None = None
    duration: str | None = None
    location: str | None = None
    description: str | None = None
    reason: str | None = None
    cost: float | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    dependency_ids: list[str] = Field(default_factory=list)
    status: str = "estimate"


class ValidationResult(FlexibleModel):
    status: str | None = None
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


class RevisionComparison(FlexibleModel):
    card_id: str
    before: ItineraryCard
    after: ItineraryCard | None = None


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str | None = None
    workflow_status: str | None = None
    current_stage: str | None = None
    pending_question: str | None = None
    trip_brief: dict = Field(default_factory=dict)
    candidate_destinations: list[dict] = Field(default_factory=list)
    time_candidates: list[TimeCandidate] = Field(default_factory=list)
    draft_cards: list[ItineraryCard] = Field(default_factory=list)
    card_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    validation_results: dict[str, ValidationResult] = Field(default_factory=dict)
    affected_cards: list[int] = Field(default_factory=list)
    modification_scope: str | None = None
    revision_comparison: list[RevisionComparison] = Field(default_factory=list)
    specialist_results: list[dict] = Field(default_factory=list)
    agent_tasks: list[dict] = Field(default_factory=list)
    workflow_events: list[dict] = Field(default_factory=list)


class WorkflowTraceDataFlow(BaseModel):
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    key_payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowTraceItem(BaseModel):
    node: str
    data_flow: WorkflowTraceDataFlow
    input_state: dict[str, Any] = Field(default_factory=dict)
    output_update: dict[str, Any] = Field(default_factory=dict)
    state_passed_forward: dict[str, Any] = Field(default_factory=dict)
    next_node: str = "__end__"


class WorkflowTraceResponse(BaseModel):
    initial_state: dict[str, Any] = Field(default_factory=dict)
    trace: list[WorkflowTraceItem] = Field(default_factory=list)
    executed_nodes: list[str] = Field(default_factory=list)

