from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EligibilityGate = Literal[
    "propositional_completeness",
    "author_assertion",
    "paper_original_support",
    "argument_sufficiency",
    "fidelity",
    "author_marked_salience",
]
EligibilityVerdict = Literal["PASS", "FAIL"]
EligibilityJudgeRole = Literal["negative", "positive", "blind_tiebreak"]

ELIGIBILITY_GATES: tuple[EligibilityGate, ...] = (
    "propositional_completeness",
    "author_assertion",
    "paper_original_support",
    "argument_sufficiency",
    "fidelity",
    "author_marked_salience",
)
ELIGIBILITY_PROFILE_ID = "claim-adjudication-panel-v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EligibilityGateAssessment(_StrictModel):
    gate: EligibilityGate
    passed: bool
    rationale: str = Field(min_length=1)
    cited_span_ids: list[str] = Field(default_factory=list)


class EligibilityCandidateAssessment(_StrictModel):
    candidate_ref: str
    claim_atoms: list[str] = Field(min_length=1)
    gates: list[EligibilityGateAssessment] = Field(min_length=len(ELIGIBILITY_GATES))
    rationale: str = Field(min_length=1)


class EligibilityAgentOutput(_StrictModel):
    assessments: list[EligibilityCandidateAssessment]


class BlindEligibilityFinding(_StrictModel):
    finding_ref: str
    statement: str = Field(min_length=1)
    cited_span_ids: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class BlindEligibilityDiscoveryOutput(_StrictModel):
    findings: list[BlindEligibilityFinding] = Field(default_factory=list)
    search_summary: str = Field(min_length=1)


class EligibilityTiebreakAssessment(EligibilityCandidateAssessment):
    matched_finding_refs: list[str] = Field(default_factory=list)
    primary_disagreement_resolution: str = ""

    @model_validator(mode="before")
    @classmethod
    def use_disagreement_resolution_as_rationale(cls, value):
        if not isinstance(value, dict):
            return value
        if value.get("rationale") or not value.get("primary_disagreement_resolution"):
            return value
        return {
            **value,
            "rationale": value["primary_disagreement_resolution"],
        }


class EligibilityTiebreakOutput(_StrictModel):
    assessments: list[EligibilityTiebreakAssessment]


class CandidateEligibilityVote(BaseModel):
    candidate_id: str
    judge_role: EligibilityJudgeRole
    verdict: EligibilityVerdict
    claim_atoms: list[str] = Field(default_factory=list)
    gates: list[EligibilityGateAssessment]
    rationale: str
    model: str = ""


class CandidateEligibilityDecision(BaseModel):
    candidate_id: str
    verdict: EligibilityVerdict
    consensus_route: Literal["unanimous", "blind_tiebreak"]
    primary_votes: list[CandidateEligibilityVote]
    tiebreak_vote: CandidateEligibilityVote | None = None
    failed_gates: list[EligibilityGate] = Field(default_factory=list)
    cited_span_ids: list[str] = Field(default_factory=list)
    rationale: str


def validate_eligibility_output(
    output: EligibilityAgentOutput | EligibilityTiebreakOutput,
    *,
    expected_candidate_refs: set[str],
) -> None:
    refs = [assessment.candidate_ref for assessment in output.assessments]
    if len(refs) != len(set(refs)):
        raise ValueError("Eligibility output contains duplicate candidate references.")
    if set(refs) != expected_candidate_refs:
        missing = sorted(expected_candidate_refs.difference(refs))
        unexpected = sorted(set(refs).difference(expected_candidate_refs))
        raise ValueError(
            "Eligibility output does not cover the exact candidate set: "
            f"missing={missing} unexpected={unexpected}."
        )
    expected_gates = set(ELIGIBILITY_GATES)
    for assessment in output.assessments:
        gates = [item.gate for item in assessment.gates]
        if len(gates) != len(set(gates)) or set(gates) != expected_gates:
            raise ValueError(
                f"Eligibility candidate {assessment.candidate_ref} must contain each hard gate exactly once."
            )


def vote_from_assessment(
    *,
    candidate_id: str,
    judge_role: EligibilityJudgeRole,
    assessment: EligibilityCandidateAssessment,
    model: str,
) -> CandidateEligibilityVote:
    return CandidateEligibilityVote(
        candidate_id=candidate_id,
        judge_role=judge_role,
        verdict="PASS" if all(gate.passed for gate in assessment.gates) else "FAIL",
        claim_atoms=list(assessment.claim_atoms),
        gates=list(assessment.gates),
        rationale=assessment.rationale,
        model=model,
    )


def decide_candidate_eligibility(
    *,
    candidate_id: str,
    negative_vote: CandidateEligibilityVote,
    positive_vote: CandidateEligibilityVote,
    tiebreak_vote: CandidateEligibilityVote | None = None,
) -> CandidateEligibilityDecision:
    if negative_vote.candidate_id != candidate_id or positive_vote.candidate_id != candidate_id:
        raise ValueError("Primary eligibility vote candidate identity mismatch.")
    primary_votes = [negative_vote, positive_vote]
    if negative_vote.verdict == positive_vote.verdict:
        final_vote = negative_vote
        route: Literal["unanimous", "blind_tiebreak"] = "unanimous"
        rationale = (
            f"Primary judges agreed on {final_vote.verdict}. "
            f"Negative: {negative_vote.rationale} Positive: {positive_vote.rationale}"
        )
    else:
        if tiebreak_vote is None:
            raise ValueError("Split eligibility votes require a blind tiebreak vote.")
        if tiebreak_vote.candidate_id != candidate_id:
            raise ValueError("Eligibility tiebreak vote candidate identity mismatch.")
        final_vote = tiebreak_vote
        route = "blind_tiebreak"
        rationale = (
            f"Primary judges split ({negative_vote.verdict}/{positive_vote.verdict}); "
            f"blind tiebreak decided {tiebreak_vote.verdict}. {tiebreak_vote.rationale}"
        )
    failed_gates = sorted(
        {gate.gate for gate in final_vote.gates if not gate.passed},
        key=ELIGIBILITY_GATES.index,
    )
    cited_span_ids = sorted(
        {
            span_id
            for gate in final_vote.gates
            for span_id in gate.cited_span_ids
            if span_id
        }
    )
    return CandidateEligibilityDecision(
        candidate_id=candidate_id,
        verdict=final_vote.verdict,
        consensus_route=route,
        primary_votes=primary_votes,
        tiebreak_vote=tiebreak_vote,
        failed_gates=failed_gates,
        cited_span_ids=cited_span_ids,
        rationale=rationale,
    )
