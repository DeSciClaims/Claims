import pytest

from competition.failure_policy import incumbent_failure_summary


class Backend:
    def __init__(self, statuses):
        self.statuses = statuses

    def list_paper_jobs(self, _round_id, _stage, paper_id):
        return [{"hotkey": "incumbent", "status": "completed",
                 "result_private": {"status": self.statuses[paper_id]}}]


def test_strict_twenty_percent_boundary_and_shared_fault_exclusion():
    papers = [f"paper-{index}" for index in range(5)]
    statuses = {paper: "completed" for paper in papers}
    statuses[papers[0]] = "package_failed"
    backend = Backend(statuses)
    exactly = incumbent_failure_summary(
        "week", stage="final", paper_ids=papers,
        excluded_infrastructure_papers=set(), incumbent_hotkey="incumbent",
        backend=backend,
    )
    assert (exactly.failed_papers, exactly.eligible_papers) == (1, 5)
    assert not exactly.reference_required
    statuses[papers[1]] = "cap_exceeded"
    assert incumbent_failure_summary(
        "week", stage="final", paper_ids=papers,
        excluded_infrastructure_papers=set(), incumbent_hotkey="incumbent",
        backend=backend,
    ).reference_required
    statuses[papers[4]] = "infrastructure_fault"
    excluded = incumbent_failure_summary(
        "week", stage="final", paper_ids=papers,
        excluded_infrastructure_papers={papers[4]}, incumbent_hotkey="incumbent",
        backend=backend,
    )
    assert (excluded.failed_papers, excluded.eligible_papers) == (2, 4)


def test_unexcluded_baseline_infrastructure_fault_is_rejected():
    backend = Backend({"paper": "infrastructure_fault"})
    with pytest.raises(ValueError, match="not excluded globally"):
        incumbent_failure_summary(
            "week", stage="screen", paper_ids=["paper"],
            excluded_infrastructure_papers=set(), incumbent_hotkey="incumbent",
            backend=backend,
        )
