from __future__ import annotations

from difflib import SequenceMatcher

from .comparison_models import ComparisonCandidate, CandidatePairEdge, RelationType


def classify_candidate_pair(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
    confidence = _similarity(left.normalized_statement, right.normalized_statement)
    relation: RelationType
    if left.normalized_statement == right.normalized_statement:
        relation = "semantic_equivalent"
        confidence = 1.0
    elif _contains_numbers(left.normalized_statement) and _contains_numbers(right.normalized_statement) and _numbers(left.normalized_statement) != _numbers(right.normalized_statement):
        relation = "contradiction"
        confidence = max(confidence, 0.75)
    elif confidence >= 0.86:
        relation = "semantic_equivalent"
    elif confidence >= 0.72:
        relation = "compatible_refinement"
    elif confidence >= 0.55:
        relation = "partial_overlap"
    else:
        relation = "unrelated"
    return CandidatePairEdge(
        edge_id=f"{left.candidate_id}::{right.candidate_id}",
        left_candidate_id=left.candidate_id,
        right_candidate_id=right.candidate_id,
        relation=relation,
        confidence=round(confidence, 4),
    )


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return max(SequenceMatcher(None, left, right).ratio(), 0.82)
    return SequenceMatcher(None, left, right).ratio()


def _contains_numbers(value: str) -> bool:
    return any(char.isdigit() for char in value)


def _numbers(value: str) -> list[str]:
    return [part for part in value.split() if any(char.isdigit() for char in part)]
