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
    backend.status = "gold_closed"
    veto = GoldResult(60, 60, 45, 0.75, True)
    refused = select_ready_final("week2", backend=backend,
                                 gold_by_hotkey={"challenger": veto}, **checks)
    assert refused.production_hotkey == "incumbent"
    assert refused.verdicts[0].reason == "gold_refused"
