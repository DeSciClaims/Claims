"""Output-level copy backstop on accepted classes and paired paper scores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .policy import PaperPairScore, paired_result


@dataclass(frozen=True)
class CopyVerdict:
    common_papers: int
    jaccard: float
    mean_gain: float
    standard_error: float
    is_copy: bool


def output_copy_verdict(
    *, candidate_classes: Mapping[str, set[str]],
    comparator_classes: Mapping[str, set[str]],
    paired_scores: list[PaperPairScore],
    threshold: float = 0.85,
) -> CopyVerdict:
    """A copy shares >=85% accepted classes without a >2 SE gain.

    Class IDs must be canonical IDs from the same paper's full adjudication.
    Scores must be computed in the pairwise context, with shared
    infrastructure-fault papers marked for exclusion on both sides.
    """
    if not 0 <= threshold <= 1:
        raise ValueError("copy threshold must be between zero and one")
    common = sorted(set(candidate_classes) & set(comparator_classes))
    if not common:
        raise ValueError("copy check requires common evaluated papers")
    score_map = {score.paper_id: score for score in paired_scores}
    if len(score_map) != len(paired_scores) or set(score_map) != set(common):
        raise ValueError("paired scores must cover exactly the common papers")
    accepted_candidate: set[tuple[str, str]] = set()
    accepted_comparator: set[tuple[str, str]] = set()
    for paper_id in common:
        if score_map[paper_id].infrastructure_fault:
            continue
        for class_id in candidate_classes[paper_id]:
            if not class_id:
                raise ValueError("accepted class ID cannot be empty")
            accepted_candidate.add((paper_id, class_id))
        for class_id in comparator_classes[paper_id]:
            if not class_id:
                raise ValueError("accepted class ID cannot be empty")
            accepted_comparator.add((paper_id, class_id))
    union = accepted_candidate | accepted_comparator
    overlap = len(accepted_candidate & accepted_comparator) / len(union) if union else 1.0
    result = paired_result(paired_scores)
    return CopyVerdict(
        common_papers=result.paper_count,
        jaccard=overlap,
        mean_gain=result.mean_gain,
        standard_error=result.standard_error,
        is_copy=overlap >= threshold and not result.mean_gain > 2 * result.standard_error,
    )
