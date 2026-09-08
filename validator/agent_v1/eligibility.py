from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EligibilityGate = Literal[
    "propositional_completeness",
    "author_assertion",
    "paper_original_support",
    "argument_sufficiency",
    "fidelity",
    "author_marked_salience",
]
EligibilityAdjudicationJudgeRole = Literal["negative", "positive", "tiebreak"]

ELIGIBILITY_GATES: tuple[EligibilityGate, ...] = (
    "propositional_completeness",
    "author_assertion",
    "paper_original_support",
    "argument_sufficiency",
    "fidelity",
    "author_marked_salience",
)
EVIDENCE_REQUIRED_GATES: tuple[EligibilityGate, ...] = (
    "paper_original_support",
)
ELIGIBILITY_PROFILE_ID = "claim-adjudication-panel-v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EligibilityGateAssessment(_StrictModel):
    gate: EligibilityGate
    passed: bool
    rationale: str = Field(min_length=1)
    cited_span_ids: list[str] = Field(default_factory=list)


class EligibilityClaimAtomAssessment(_StrictModel):
    atom: str = Field(min_length=1)
    supported: bool
    cited_span_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class EligibilityCandidateAssessment(_StrictModel):
    candidate_ref: str
    claim_atoms: list[str] = Field(min_length=1)
    claim_atom_assessments: list[EligibilityClaimAtomAssessment] = Field(min_length=1)
    gates: list[EligibilityGateAssessment] = Field(min_length=len(ELIGIBILITY_GATES))
    rationale: str = Field(min_length=1)


class EligibilityAdjudicationCaseAssessment(_StrictModel):
    case_ref: str
    candidate_assessments: list[EligibilityCandidateAssessment] = Field(
        min_length=1,
        max_length=2,
    )
    selected_candidate_ref: str | None = None
    rationale: str = Field(min_length=1)


class EligibilityAdjudicationAgentOutput(_StrictModel):
    assessments: list[EligibilityAdjudicationCaseAssessment]


class EligibilityAdjudicationVote(BaseModel):
    case_id: str
    judge_role: EligibilityAdjudicationJudgeRole
    selected_candidate_id: str | None = None
    candidate_assessments: list[EligibilityCandidateAssessment]
    rationale: str
    model: str = ""


class EligibilityAdjudicationDecision(BaseModel):
    case_id: str
    selected_candidate_id: str | None = None
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    consensus_route: Literal["unanimous", "tiebreak"]
    primary_votes: list[EligibilityAdjudicationVote]
    tiebreak_vote: EligibilityAdjudicationVote | None = None
    cited_span_ids: list[str] = Field(default_factory=list)
    rationale: str


def validate_candidate_assessments(
    assessments: list[EligibilityCandidateAssessment],
    *,
    expected_candidate_refs: set[str],
) -> None:
    refs = [assessment.candidate_ref for assessment in assessments]
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
    for assessment in assessments:
        atoms = [atom.strip() for atom in assessment.claim_atoms]
        assessed_atoms = [item.atom.strip() for item in assessment.claim_atom_assessments]
        if (
            any(not atom for atom in atoms)
            or len(atoms) != len(set(atoms))
            or len(assessed_atoms) != len(set(assessed_atoms))
            or set(assessed_atoms) != set(atoms)
        ):
            raise ValueError(
                f"Eligibility candidate {assessment.candidate_ref} must assess every "
                "distinct claim atom exactly once."
            )
        gates = [item.gate for item in assessment.gates]
        if len(gates) != len(set(gates)) or set(gates) != expected_gates:
            raise ValueError(
                f"Eligibility candidate {assessment.candidate_ref} must contain each hard gate exactly once."
            )


def validate_eligibility_adjudication_output(
    output: EligibilityAdjudicationAgentOutput,
    *,
    expected_candidate_refs_by_case: dict[str, set[str]],
) -> None:
    case_refs = [assessment.case_ref for assessment in output.assessments]
    if len(case_refs) != len(set(case_refs)):
        raise ValueError("Eligibility adjudication output contains duplicate case references.")
    if set(case_refs) != set(expected_candidate_refs_by_case):
        missing = sorted(set(expected_candidate_refs_by_case).difference(case_refs))
        unexpected = sorted(set(case_refs).difference(expected_candidate_refs_by_case))
        raise ValueError(
            "Eligibility adjudication output does not cover the exact case set: "
            f"missing={missing} unexpected={unexpected}."
        )
    for case in output.assessments:
        expected_refs = expected_candidate_refs_by_case[case.case_ref]
        validate_candidate_assessments(
            case.candidate_assessments,
            expected_candidate_refs=expected_refs,
        )
        if case.selected_candidate_ref is not None and case.selected_candidate_ref not in expected_refs:
            raise ValueError(
                f"Eligibility adjudication case {case.case_ref} selected an unknown candidate."
            )
        passing_refs = [
            assessment.candidate_ref
            for assessment in case.candidate_assessments
            if assessment_passes_hard_gates(normalize_eligibility_assessment(assessment))
        ]
        if len(passing_refs) == 2 and case.selected_candidate_ref is None:
            raise ValueError(
                f"Eligibility adjudication case {case.case_ref} must state a preference "
                "when both candidates pass."
            )


def normalize_eligibility_assessment(
    assessment: EligibilityCandidateAssessment,
) -> EligibilityCandidateAssessment:
    atom_assessments = [
        item.model_copy(
            update={
                "supported": False,
                "rationale": (
                    f"{item.rationale} Validator policy rejected this atom because the "
                    "judge supplied no direct source-span citation."
                ),
            }
        )
        if item.supported and not item.cited_span_ids
        else item
        for item in assessment.claim_atom_assessments
    ]
    unsupported_atoms = [item for item in atom_assessments if not item.supported]
    atom_span_ids = sorted(
        {
            span_id
            for item in atom_assessments
            if item.supported
            for span_id in item.cited_span_ids
            if span_id
        }
    )
    normalized_gates: list[EligibilityGateAssessment] = []
    for gate in assessment.gates:
        if (
            gate.passed
            and gate.gate in EVIDENCE_REQUIRED_GATES
            and not gate.cited_span_ids
            and atom_span_ids
        ):
            gate = gate.model_copy(
                update={
                    "cited_span_ids": atom_span_ids,
                    "rationale": (
                        f"{gate.rationale} Validator policy reused the direct claim-atom "
                        "citations for this gate."
                    ),
                }
            )
        rejection_reason = ""
        if gate.passed and gate.gate in EVIDENCE_REQUIRED_GATES and not gate.cited_span_ids:
            rejection_reason = "the judge supplied no decisive source-span citation"
        elif (
            gate.passed
            and unsupported_atoms
            and gate.gate in {"argument_sufficiency", "fidelity"}
        ):
            rejection_reason = "at least one material claim atom lacks direct cited support"
        if rejection_reason:
            gate = gate.model_copy(
                update={
                    "passed": False,
                    "rationale": (
                        f"{gate.rationale} Validator policy rejected this gate because "
                        f"{rejection_reason}."
                    ),
                }
            )
        normalized_gates.append(gate)
    return assessment.model_copy(
        update={
            "claim_atom_assessments": atom_assessments,
            "gates": normalized_gates,
        }
    )


def assessment_passes_hard_gates(assessment: EligibilityCandidateAssessment) -> bool:
    atoms_pass = bool(assessment.claim_atom_assessments) and all(
        item.supported and bool(item.cited_span_ids)
        for item in assessment.claim_atom_assessments
    )
    gates_pass = all(
        gate.passed
        and (
            gate.gate not in EVIDENCE_REQUIRED_GATES
            or bool(gate.cited_span_ids)
        )
        for gate in assessment.gates
    )
    return atoms_pass and gates_pass


def eligibility_adjudication_vote_from_assessment(
    *,
    case_id: str,
    judge_role: EligibilityAdjudicationJudgeRole,
    assessment: EligibilityAdjudicationCaseAssessment,
    candidate_id_by_ref: dict[str, str],
    model: str,
) -> EligibilityAdjudicationVote:
    normalized_assessments = [
        normalize_eligibility_assessment(item).model_copy(
            update={"candidate_ref": candidate_id_by_ref[item.candidate_ref]}
        )
        for item in assessment.candidate_assessments
    ]
    passing_candidate_ids = [
        item.candidate_ref
        for item in normalized_assessments
        if assessment_passes_hard_gates(item)
    ]
    if not passing_candidate_ids:
        selected_candidate_id = None
        derived_reason = "Validator policy selected neither because every candidate failed."
    elif len(passing_candidate_ids) == 1:
        selected_candidate_id = passing_candidate_ids[0]
        derived_reason = (
            "Validator policy selected the only candidate that passed every atom and hard gate."
        )
    else:
        selected_candidate_id = (
            candidate_id_by_ref[assessment.selected_candidate_ref]
            if assessment.selected_candidate_ref is not None
            else None
        )
        derived_reason = "Both candidates passed; the judge preference determined selection."
    return EligibilityAdjudicationVote(
        case_id=case_id,
        judge_role=judge_role,
        selected_candidate_id=selected_candidate_id,
        candidate_assessments=normalized_assessments,
        rationale=f"{assessment.rationale} {derived_reason}",
        model=model,
    )


def decide_eligibility_adjudication(
    *,
    case_id: str,
    candidate_ids: list[str],
    negative_vote: EligibilityAdjudicationVote,
    positive_vote: EligibilityAdjudicationVote,
    tiebreak_vote: EligibilityAdjudicationVote | None = None,
) -> EligibilityAdjudicationDecision:
    if len(candidate_ids) not in {1, 2} or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("Eligibility adjudication requires one or two distinct candidates.")
    if negative_vote.case_id != case_id or positive_vote.case_id != case_id:
        raise ValueError("Primary eligibility adjudication vote case identity mismatch.")
    primary_votes = [negative_vote, positive_vote]
    if negative_vote.selected_candidate_id == positive_vote.selected_candidate_id:
        final_vote = negative_vote
        route: Literal["unanimous", "tiebreak"] = "unanimous"
        rationale = (
            f"Primary judges agreed on {final_vote.selected_candidate_id or 'neither'}. "
            f"Negative: {negative_vote.rationale} Positive: {positive_vote.rationale}"
        )
    else:
        if tiebreak_vote is None:
            raise ValueError("Split eligibility adjudication votes require a tiebreak vote.")
        if tiebreak_vote.case_id != case_id:
            raise ValueError("Eligibility adjudication tiebreak vote case identity mismatch.")
        final_vote = tiebreak_vote
        route = "tiebreak"
        rationale = (
            "Primary eligibility judges disagreed; "
            f"tiebreak selected {final_vote.selected_candidate_id or 'neither'}. "
            f"{tiebreak_vote.rationale}"
        )
    if final_vote.selected_candidate_id not in {*candidate_ids, None}:
        raise ValueError("Eligibility adjudication selected a candidate outside the case.")
    selected_assessment = next(
        (
            assessment
            for assessment in final_vote.candidate_assessments
            if assessment.candidate_ref == final_vote.selected_candidate_id
        ),
        None,
    )
    cited_span_ids = sorted(
        {
            span_id
            for assessment in (
                [selected_assessment]
                if selected_assessment is not None
                else final_vote.candidate_assessments
            )
            for span_id in [
                *(span_id for gate in assessment.gates for span_id in gate.cited_span_ids),
                *(
                    span_id
                    for atom in assessment.claim_atom_assessments
                    for span_id in atom.cited_span_ids
                ),
            ]
            if span_id
        }
    )
    return EligibilityAdjudicationDecision(
        case_id=case_id,
        selected_candidate_id=final_vote.selected_candidate_id,
        rejected_candidate_ids=[
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id != final_vote.selected_candidate_id
        ],
        consensus_route=route,
        primary_votes=primary_votes,
        tiebreak_vote=tiebreak_vote,
        cited_span_ids=cited_span_ids,
        rationale=rationale,
    )
