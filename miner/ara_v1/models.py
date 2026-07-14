from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AraSourceRef(BaseModel):
    source_id: str
    source_type: str
    path: str | None = None
    span_ids: list[str] = Field(default_factory=list)
    quote: str | None = None
    role: Literal["input", "result", "method", "interpretation", "metadata"] = "input"


class AraPaper(BaseModel):
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


class AraClaim(BaseModel):
    claim_id: str
    statement: str
    conditions: str
    status: str
    falsification_criteria: str
    proof: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    sources: list[AraSourceRef] = Field(default_factory=list)
    source_claim_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AraConcept(BaseModel):
    concept_id: str
    label: str
    definition: str
    source_refs: list[AraSourceRef] = Field(default_factory=list)


class AraExperiment(BaseModel):
    experiment_id: str
    title: str
    verifies: list[str] = Field(default_factory=list)
    setup: str
    procedure: str
    expected_outcome: str
    evidence_ids: list[str] = Field(default_factory=list)
    run: str | None = None
    source_refs: list[AraSourceRef] = Field(default_factory=list)


class AraEvidenceRecord(BaseModel):
    evidence_id: str
    title: str
    role: str
    summary: str
    evidence_method: str = ""
    outcome_type: str | None = None
    presentation_type: str | None = None
    source_refs: list[AraSourceRef] = Field(default_factory=list)
    linked_claim_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AraTraceNode(BaseModel):
    node_id: str
    node_type: str
    support_level: Literal["explicit", "inferred"] = "inferred"
    summary: str
    source_refs: list[AraSourceRef] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    children: list["AraTraceNode"] = Field(default_factory=list)


class AraLogic(BaseModel):
    problem_observations: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    key_insight: str = ""
    assumptions: list[str] = Field(default_factory=list)
    claims: list[AraClaim] = Field(default_factory=list)
    concepts: list[AraConcept] = Field(default_factory=list)
    experiments: list[AraExperiment] = Field(default_factory=list)
    related_work: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class AraEvidenceLayer(BaseModel):
    records: list[AraEvidenceRecord] = Field(default_factory=list)
    ledger_notes: list[str] = Field(default_factory=list)


class AraSrcLayer(BaseModel):
    environment: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class AraArtifact(BaseModel):
    ara_version: str = "1.0"
    paper: AraPaper
    logic: AraLogic
    evidence: AraEvidenceLayer
    trace: AraTraceNode
    src: AraSrcLayer
    metadata: dict[str, Any] = Field(default_factory=dict)


AraTraceNode.model_rebuild()
