from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import AgentV1ValidationFinding


CandidateOrigin = Literal["bronze", "miner"]
RelationType = Literal[
    "semantic_equivalent",
    "compatible_refinement",
    "compatible_split_merge",
    "partial_overlap",
    "contradiction",
    "unrelated",
    "insufficient_context",
]
MismatchType = Literal[
    "SEMANTIC_EQUIVALENCE_UNCERTAIN",
    "MISSING_FROM_MINER",
    "EXTRA_FROM_MINER",
    "QUALIFIER_CONFLICT",
    "COMPATIBLE_REFINEMENT",
    "CONTRADICTION",
    "EVIDENCE_DISAGREEMENT",
]
Importance = Literal["central", "supporting", "minor"]


class ComparisonCandidate(BaseModel):
    candidate_id: str
    paper_id: str | None = None
    origin: CandidateOrigin
    miner_id: str | None = None
    record_id: str
    statement: str
    normalized_statement: str
    qualifier: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    source_span_ids: list[str] = Field(default_factory=list)
    source_quotes: list[str] = Field(default_factory=list)
    importance: Importance = "supporting"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidatePairEdge(BaseModel):
    edge_id: str
    left_candidate_id: str
    right_candidate_id: str
    relation: RelationType
    confidence: float = 0.0
    rationale: str | None = None


class BronzeDiffCase(BaseModel):
    case_id: str
    paper_id: str
    miner_id: str
    mismatch_type: MismatchType
    candidate_ids: list[str]
    bronze_candidate_id: str | None = None
    miner_candidate_id: str | None = None
    question: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SilverUnit(BaseModel):
    silver_unit_id: str
    paper_id: str
    statement: str
    importance: Importance = "supporting"
    required_for_completeness: bool = True
    equivalent_candidate_ids: list[str] = Field(default_factory=list)
    source_span_ids: list[str] = Field(default_factory=list)
    adjudication_case_ids: list[str] = Field(default_factory=list)
    scoring_mode: Literal["required", "accepted_improvement"] = "required"
    metadata: dict[str, Any] = Field(default_factory=dict)


class InvalidMinerCandidate(BaseModel):
    candidate_id: str
    miner_id: str
    reason: str
    adjudication_case_id: str | None = None
    severity: Literal["critical", "major", "minor"] = "critical"
    evidence_span: str | None = None


class ReferenceError(BaseModel):
    candidate_id: str
    reason: str
    adjudication_case_id: str | None = None


class SilverRecord(BaseModel):
    silver_record_id: str
    paper_id: str
    bronze_record_id: str | None = None
    silver_units: list[SilverUnit] = Field(default_factory=list)
    invalid_miner_candidates: list[InvalidMinerCandidate] = Field(default_factory=list)
    reference_errors: list[ReferenceError] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SilverScoreBreakdown(BaseModel):
    paper_id: str
    miner_id: str
    silver_record_id: str
    coverage: float
    quality: float
    score: float
    covered_required_silver_units: list[str] = Field(default_factory=list)
    missing_required_silver_units: list[str] = Field(default_factory=list)
    accepted_improvements: list[str] = Field(default_factory=list)
    invalid_extra_candidates: list[str] = Field(default_factory=list)
    findings: list[AgentV1ValidationFinding] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
