from __future__ import annotations

import hashlib

from .comparison_models import BronzeDiffCase, ComparisonCandidate, MismatchType
from .pairing import RelationClassifier, build_candidate_pairs


def compare_miner_to_bronze(
    *,
    paper_id: str,
    miner_id: str,
    bronze_candidates: list[ComparisonCandidate],
    miner_candidates: list[ComparisonCandidate],
    relation_classifier: RelationClassifier | None = None,
) -> list[BronzeDiffCase]:
    edges = build_candidate_pairs(bronze_candidates, miner_candidates, relation_classifier=relation_classifier)
    best_by_bronze: dict[str, tuple[str, str]] = {}
    matched_miner_ids: set[str] = set()

    for edge in edges:
        if edge.left_candidate_id in best_by_bronze:
            continue
        best_by_bronze[edge.left_candidate_id] = (edge.right_candidate_id, edge.relation)
        matched_miner_ids.add(edge.right_candidate_id)

    cases: list[BronzeDiffCase] = []
    for bronze in bronze_candidates:
        match = best_by_bronze.get(bronze.candidate_id)
        if match is None:
            cases.append(
                _case(
                    paper_id=paper_id,
                    miner_id=miner_id,
                    mismatch_type="MISSING_FROM_MINER",
                    candidate_ids=[bronze.candidate_id],
                    bronze_candidate_id=bronze.candidate_id,
                    miner_candidate_id=None,
                    question="Is this Bronze-only candidate valid and required?",
                )
            )
            continue
        miner_candidate_id, relation = match
        if relation == "semantic_equivalent":
            continue
        cases.append(
            _case(
                paper_id=paper_id,
                miner_id=miner_id,
                mismatch_type=_mismatch_type(relation),
                candidate_ids=[bronze.candidate_id, miner_candidate_id],
                bronze_candidate_id=bronze.candidate_id,
                miner_candidate_id=miner_candidate_id,
                question="How should this Bronze/miner disagreement affect Silver?",
            )
        )

    for miner in miner_candidates:
        if miner.candidate_id in matched_miner_ids:
            continue
        cases.append(
            _case(
                paper_id=paper_id,
                miner_id=miner_id,
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[miner.candidate_id],
                bronze_candidate_id=None,
                miner_candidate_id=miner.candidate_id,
                question="Is this miner-only candidate a valid improvement or an invalid extra?",
            )
        )
    return cases


def _mismatch_type(relation: str) -> MismatchType:
    if relation == "compatible_refinement":
        return "COMPATIBLE_REFINEMENT"
    if relation == "contradiction":
        return "CONTRADICTION"
    return "SEMANTIC_EQUIVALENCE_UNCERTAIN"


def _case(
    *,
    paper_id: str,
    miner_id: str,
    mismatch_type: str,
    candidate_ids: list[str],
    bronze_candidate_id: str | None,
    miner_candidate_id: str | None,
    question: str,
) -> BronzeDiffCase:
    digest = hashlib.sha256("|".join([paper_id, miner_id, mismatch_type, *candidate_ids]).encode("utf-8")).hexdigest()[:16]
    return BronzeDiffCase(
        case_id=f"bronze_diff_{digest}",
        paper_id=paper_id,
        miner_id=miner_id,
        mismatch_type=mismatch_type,  # type: ignore[arg-type]
        candidate_ids=candidate_ids,
        bronze_candidate_id=bronze_candidate_id,
        miner_candidate_id=miner_candidate_id,
        question=question,
    )
