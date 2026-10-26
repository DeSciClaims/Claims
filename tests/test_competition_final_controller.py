import pytest

from competition.final_controller import select_ready_final
from competition.gold import GoldResult


class Backend:
    def __init__(self):
        self.status = "final"
        self.pairs = [{
            "stage": "final", "paper_id": f"paper-{index}",
            "challenger_hotkey": "challenger", "baseline_hotkey": "incumbent",
            "challenger_package_sha256": "f" * 64,
            "baseline_package_sha256": "c" * 64,
            "challenger_score": 0.55, "baseline_score": 0.50,
            "infrastructure_fault": False,
        } for index in range(5)]

    def get_round(self, _round_id):
        return {"status": self.status, "incumbent_hotkey": "incumbent"}

    def list_stages(self, _round_id):
        return [{"stage": "final", "entries": [
            {"role": "incumbent", "hotkey": "incumbent", "team_id": "incumbent-team",
             "package_sha256": "c" * 64},
            {"role": "challenger", "hotkey": "challenger", "team_id": "other-team",
             "package_sha256": "f" * 64},
        ]}]

    def list_sample(self, _round_id):
        return [{"stage": "final", "paper_id": f"paper-{index}"} for index in range(5)]

    def list_stage_pairs(self, _round_id, _stage):
        return self.pairs

    def list_submissions(self, _round_id):
        return [{"hotkey": "challenger", "status": "accepted", "commit_block": 120}]

    def list_paper_jobs(self, _round_id, _stage, _paper_id):
        return [{"hotkey": "incumbent", "status": "completed",
                 "result_private": {"status": "completed"}}]

    def list_gold_cases(self, _round_id, *, include_answers=False):
        return []


def test_final_controller_requires_complete_grid_and_all_verdicts():
    backend = Backend()
    with pytest.raises(ValueError, match="verdicts"):
        select_ready_final("week2", backend=backend, code_passed_by_hotkey={},
                           copy_by_hotkey={}, gold_by_hotkey={})
    backend.pairs.pop()
    with pytest.raises(ValueError, match="incomplete"):
        select_ready_final("week2", backend=backend,
                           code_passed_by_hotkey={"challenger": True},
                           copy_by_hotkey={"challenger": False}, gold_by_hotkey={})


def test_closed_gold_veto_blocks_otherwise_clear_crown():
    backend = Backend()
    checks = {"code_passed_by_hotkey": {"challenger": True},
              "copy_by_hotkey": {"challenger": False}}
    selected = select_ready_final("week2", backend=backend, gold_by_hotkey={}, **checks)
    assert selected.production_hotkey == "challenger"
    assert len(selected.final_pair_rows) == 5
    backend.status = "gold_closed"
    veto = GoldResult(60, 60, 45, 0.75, True)
    refused = select_ready_final("week2", backend=backend,
                                 gold_by_hotkey={"challenger": veto}, **checks)
    assert refused.production_hotkey == "incumbent"
    assert refused.verdicts[0].reason == "gold_refused"


def test_closed_escalation_scores_feed_crown_selection(monkeypatch):
    backend = Backend()
    backend.status = "gold_closed"
    case = {
        "round_id": "week2", "case_id": "gold-1", "paper_id": "paper-0",
        "challenger_hotkey": "challenger", "incumbent_hotkey": "incumbent",
        "excluded_team_ids": ["incumbent-team", "other-team"],
        "case_public": {},
        "answer_private": {"silver_unit_id": "unit-1", "tiebreak": "challenger",
                           "review_purposes": ["escalation"]},
    }
    backend.list_gold_cases = lambda _round_id, **_: [case]
    backend.list_gold_voters = lambda _round_id: [
        {"voter_hotkey": f"voter-{index}", "team_id": f"outside-{index}"}
        for index in range(12)
    ]
    backend.list_gold_votes = lambda _round_id: [
        {"case_id": "gold-1", "voter_hotkey": f"voter-{index}", "choice": "incumbent"}
        for index in range(12)
    ]
    backend.get_paper_evaluation = lambda _round_id, _stage, _paper_id: {
        "result_private": {"pairs": [{
            "paper_id": "paper-0", "challenger_hotkey": "challenger",
            "baseline_hotkey": "incumbent", "challenger_score": 0.55,
            "baseline_score": 0.50, "infrastructure_fault": False,
        }]},
    }
    def adjusted(_result, *, sealed_cases, decisions):
        assert sealed_cases == [case] and decisions["gold-1"].choice == "incumbent"
        return [{**backend.get_paper_evaluation("week2", "final", "paper-0")["result_private"]["pairs"][0],
                 "challenger_score": 0.0}]
    monkeypatch.setattr("competition.final_controller.rescore_final_paper_from_escalations", adjusted)
    selected = select_ready_final(
        "week2", backend=backend, code_passed_by_hotkey={"challenger": True},
        copy_by_hotkey={"challenger": False},
        gold_by_hotkey={"challenger": GoldResult(0, 0, 0, None, False)},
    )
    assert selected.production_hotkey == "incumbent"
    assert selected.verdicts[0].mean_gain < 0
    assert selected.final_pair_rows[0]["challenger_score"] == 0.0


def test_final_controller_requires_reference_if_incumbent_fails_over_one_fifth():
    from competition.failure_policy import ReferenceFallbackRequired

    backend = Backend()

    def jobs(_round_id, _stage, paper_id):
        status = "package_failed" if paper_id in {"paper-0", "paper-1"} else "completed"
        return [{"hotkey": "incumbent", "status": "completed",
                 "result_private": {"status": status}}]

    backend.list_paper_jobs = jobs
    with pytest.raises(ReferenceFallbackRequired) as exc:
        select_ready_final("week2", backend=backend,
                           code_passed_by_hotkey={"challenger": True},
                           copy_by_hotkey={"challenger": False}, gold_by_hotkey={})
    assert (exc.value.summary.failed_papers, exc.value.summary.eligible_papers) == (2, 5)
