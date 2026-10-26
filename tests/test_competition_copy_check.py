import pytest

from competition.copy_check import output_copy_verdict
from competition.policy import PaperPairScore


def test_high_overlap_without_proven_gain_counts_as_copy():
    papers = [f"p{i}" for i in range(30)]
    classes = {paper: {"shared", "other"} for paper in papers}
    scores = [PaperPairScore(paper, 0.6, 0.6) for paper in papers]
    result = output_copy_verdict(
        candidate_classes=classes, comparator_classes=classes, paired_scores=scores,
    )
    assert result.jaccard == 1.0
    assert result.is_copy


def test_new_accepted_classes_or_proven_gain_clear_copy_check():
    papers = [f"p{i}" for i in range(30)]
    candidate = {paper: {"shared", "new"} for paper in papers}
    comparator = {paper: {"shared", "old"} for paper in papers}
    scores = [PaperPairScore(paper, 0.8, 0.6) for paper in papers]
    different = output_copy_verdict(
        candidate_classes=candidate, comparator_classes=comparator,
        paired_scores=scores,
    )
    assert different.jaccard == pytest.approx(1 / 3)
    assert not different.is_copy
    same = output_copy_verdict(
        candidate_classes=candidate, comparator_classes=candidate,
        paired_scores=scores,
    )
    assert not same.is_copy


def test_copy_check_uses_only_common_nonfault_papers():
    result = output_copy_verdict(
        candidate_classes={"a": {"x"}, "b": {"y"}, "extra": {"z"}},
        comparator_classes={"a": {"x"}, "b": {"q"}},
        paired_scores=[PaperPairScore("a", 0.4, 0.4), PaperPairScore("b", 0.8, 0.1, infrastructure_fault=True)],
    )
    assert result.common_papers == 1
    assert result.jaccard == 1
    assert result.is_copy
