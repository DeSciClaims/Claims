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


@pytest.mark.parametrize('gate,status,failures',[(None,'completed',0),(None,'infrastructure_fault',0),(0,'completed',1)])
def test_first_round_uncertainty_is_scored_but_not_a_confirmed_incumbent_failure(gate,status,failures):
    from competition.scientific_first_round import first_round_policy
    backend=Backend({'paper':status})
    backend.get_round=lambda _: {'rules_public':{'configuration':{'scientific_scoring':{
        'first_round_scoring':first_round_policy('week')}}}}
    backend.get_paper_evaluation=lambda *_: {'result_private':{'silver':{
        'protocol_id':'claims-source-task-v1','first_round_score':{'sha256':'a'*64},
        'output_alias_by_hotkey':{'incumbent':'a'},
        'sampled_receipt':{'deterministic_gates':[{'output_alias':'a','gate_bit':gate}]}}}}
    result=incumbent_failure_summary('week',stage='final',paper_ids=['paper'],
        excluded_infrastructure_papers=set(),incumbent_hotkey='incumbent',backend=backend)
    assert result.eligible_papers==1 and result.failed_papers==failures
