from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    source_id: str
    source_type: str
    path: str | None = None
    span_ids: list[str] = Field(default_factory=list)
    quote: str | None = None
    role: Literal["input", "result", "method", "interpretation", "metadata"] = "input"


class Paper(BaseModel):
    paper_id: str
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    domain: str | None = None
    keywords: list[str] = Field(default_factory=list)
    abstract: str = ""
    claims_summary: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    claim_id: str
    statement: str
    conditions: str
    status: str
    falsification_criteria: str
    proof: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    source_claim_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Concept(BaseModel):
    concept_id: str
    label: str
    definition: str
    source_refs: list[SourceRef] = Field(default_factory=list)


class Experiment(BaseModel):
    experiment_id: str
    title: str
    verifies: list[str] = Field(default_factory=list)
    setup: str
    procedure: str
    expected_outcome: str
    evidence_ids: list[str] = Field(default_factory=list)
    run: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    evidence_id: str
    title: str
    role: str
    summary: str
    evidence_method: str = ""
    outcome_type: str | None = None
    presentation_type: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)
    linked_claim_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceNode(BaseModel):
    node_id: str
    node_type: str
    support_level: Literal["explicit", "inferred"] = "inferred"
    summary: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    children: list["TraceNode"] = Field(default_factory=list)


class Logic(BaseModel):
    problem_observations: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    key_insight: str = ""
    assumptions: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    experiments: list[Experiment] = Field(default_factory=list)
    related_work: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class EvidenceLayer(BaseModel):
    records: list[EvidenceRecord] = Field(default_factory=list)
    ledger_notes: list[str] = Field(default_factory=list)


class SrcLayer(BaseModel):
    environment: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class Artifact(BaseModel):
    ara_version: str = "1.0"
    paper: Paper
    logic: Logic
    evidence: EvidenceLayer
    trace: TraceNode
    src: SrcLayer
    metadata: dict[str, Any] = Field(default_factory=dict)


TraceNode.model_rebuild()