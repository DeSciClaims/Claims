from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .comparison_models import BronzeDiffCase, ComparisonCandidate


AdjudicationDisposition = Literal[
    "miner_error",
    "reference_error",
    "accepted_improvement",
    "benign_difference",
    "both_valid",
    "both_invalid",
    "insufficient_information",
]
AdjudicationRoute = Literal["direct", "tiebreak", "manual_review", "miner_consensus", "unresolved"]


class AdjudicationContextBundle(BaseModel):
    case: BronzeDiffCase
    candidates: list[ComparisonCandidate]
    source_context: str = ""
    candidate_order_seed: str = ""


class AdjudicationVote(BaseModel):
    case_id: str
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    candidate_order: list[str] = Field(default_factory=list)
    disposition: AdjudicationDisposition
    material_findings: list[str] = Field(default_factory=list)
    cited_span_ids: list[str] = Field(default_factory=list)
    confidence: float
    rationale: str = ""
    insufficient_information: bool = False


class ScoreEffect(BaseModel):
    finding_code: str
    severity: Literal["critical", "major", "minor", "none"] = "none"
    applies_to_miner_ids: list[str] = Field(default_factory=list)
    note: str = ""


class AdjudicationConsensus(BaseModel):
    case_id: str
    votes: list[AdjudicationVote] = Field(default_factory=list)
    final_disposition: AdjudicationDisposition | None = None
    final_confidence: float = 0.0
    agreement_rate: float = 0.0
    route: AdjudicationRoute = "unresolved"
    score_effects: list[ScoreEffect] = Field(default_factory=list)
    rationale: str = ""


class AdjudicationDecision(BaseModel):
    case_id: str
    disposition: AdjudicationDisposition
    accepted_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    valid_alternative_candidate_ids: list[str] = Field(default_factory=list)
    silver_unit_id: str | None = None
    creates_required_silver_unit: bool = True
    creates_optional_improvement_unit: bool = False
    importance: Literal["central", "supporting", "minor"] = "supporting"
    rationale: str = ""
    consensus: AdjudicationConsensus | None = None
