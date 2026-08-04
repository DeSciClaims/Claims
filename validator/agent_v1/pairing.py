from __future__ import annotations

from collections.abc import Callable

from .comparison_models import CandidatePairEdge, ComparisonCandidate
from .relation_classifier import classify_candidate_pair

RelationClassifier = Callable[[ComparisonCandidate, ComparisonCandidate], CandidatePairEdge]
ACTIONABLE_RELATIONS = {
    "semantic_equivalent",
    "compatible_refinement",
    "compatible_split_merge",
    "partial_overlap",
    "contradiction",
}


def build_candidate_pairs(
    left: list[ComparisonCandidate],
    right: list[ComparisonCandidate],
    *,
    min_confidence: float = 0.55,
    relation_classifier: RelationClassifier | None = None,
) -> list[CandidatePairEdge]:
    classifier = relation_classifier or classify_candidate_pair
    edges: list[CandidatePairEdge] = []
    for left_candidate in left:
        for right_candidate in right:
            edge = classifier(left_candidate, right_candidate)
            if edge.confidence >= min_confidence and edge.relation in ACTIONABLE_RELATIONS:
                edges.append(edge)
    return sorted(edges, key=lambda edge: (-edge.confidence, edge.edge_id))
