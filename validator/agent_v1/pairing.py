from __future__ import annotations

from .comparison_models import CandidatePairEdge, ComparisonCandidate
from .relation_classifier import classify_candidate_pair


def build_candidate_pairs(
    left: list[ComparisonCandidate],
    right: list[ComparisonCandidate],
    *,
    min_confidence: float = 0.55,
) -> list[CandidatePairEdge]:
    edges: list[CandidatePairEdge] = []
    for left_candidate in left:
        for right_candidate in right:
            edge = classify_candidate_pair(left_candidate, right_candidate)
            if edge.confidence >= min_confidence:
                edges.append(edge)
    return sorted(edges, key=lambda edge: (-edge.confidence, edge.edge_id))
