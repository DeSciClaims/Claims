"""Independent winner selection uses the replay grid and its own final audit."""

from copy import deepcopy

import pytest

from competition.gold import GoldResult
from competition.final_controller import select_ready_final
from competition.replay_final_selection import select_replay_final


class LocalFinal:
    def __init__(self):
        self.record = {
            "round_id": "week-1", "incumbent_hotkey": None,
            "baseline_package_sha256": "b" * 64,
            "reference_package_sha256": "b" * 64,
            "configuration_sha256": "c" * 64,
        }
        self.stages = [{"stage": "week_one", "entries": [
            {"hotkey": "reference", "team_id": "team-reference",
             "package_sha256": "b" * 64, "role": "reference"},
            {"hotkey": "miner-1", "team_id": "team-miner-1",
             "package_sha256": "a" * 64, "role": "challenger"},
        ]}]
        self.sample = [{"stage": "week_one", "paper_id": "paper-1"}]
        self.pairs = [{
            "stage": "week_one", "paper_id": "paper-1",
            "challenger_hotkey": "miner-1", "baseline_hotkey": "reference",
            "challenger_package_sha256": "a" * 64,
            "baseline_package_sha256": "b" * 64,
            "challenger_score": 0.7, "baseline_score": 0.5,
            "infrastructure_fault": False,
        }]
        self.audit = {"result_private": {
            "round_id": "week-1", "stage": "week_one",
            "configuration_sha256": "c" * 64,
            "code_passed": {"miner-1": True},
            "copy_audit": {"miner-1": {"is_copy": False}},
            "worker_security_audit": {
                "reference": {"passed": True}, "miner-1": {"passed": True},
            },
        }}

    def get_round(self, round_id):
        return self.record

    def list_stages(self, round_id):
        return self.stages

    def list_sample(self, round_id):
        return self.sample

    def get_final_audit(self, round_id):
        return self.audit

    def list_stage_pairs(self, round_id, stage):
        return self.pairs

    def list_submissions(self, round_id):
        return [{"hotkey": "miner-1", "status": "accepted", "commit_block": 10}]

    def list_gold_cases(self, round_id, *, include_answers=False):
        return []


GOLD = {"miner-1": GoldResult(0, 0, 0, None, False)}


def test_replay_selection_chooses_from_its_own_audited_pairs():
    backend = LocalFinal()
    selected = select_replay_final("week-1", backend=backend, gold_by_hotkey=GOLD)
    assert selected.decision == "new_crown"
    assert selected.production_hotkey == "miner-1"
    assert selected.verdicts[0].mean_gain == pytest.approx(0.2)
    assert selected.final_pair_rows == tuple(backend.pairs)
    backend.record["status"] = "gold_closed"
    assert selected == select_ready_final(
        "week-1", backend=backend,
        code_passed_by_hotkey={"miner-1": True},
        copy_by_hotkey={"miner-1": False}, gold_by_hotkey=GOLD,
    )


def test_replay_selection_requires_complete_gold_and_pair_grid():
    backend = LocalFinal()
    with pytest.raises(ValueError, match="Gold outcome"):
        select_replay_final("week-1", backend=backend, gold_by_hotkey={})
    backend.pairs.clear()
    with pytest.raises(ValueError, match="pair grid is incomplete"):
        select_replay_final("week-1", backend=backend, gold_by_hotkey=GOLD)


def test_replay_selection_applies_gold_copy_and_security_checks():
    backend = LocalFinal()
    refused = {"miner-1": GoldResult(100, 60, 50, 50 / 60, True)}
    assert select_replay_final(
        "week-1", backend=backend, gold_by_hotkey=refused,
    ).verdicts[0].reason == "gold_refused"
    backend.audit = deepcopy(backend.audit)
    backend.audit["result_private"]["copy_audit"]["miner-1"]["is_copy"] = True
    assert select_replay_final(
        "week-1", backend=backend, gold_by_hotkey=GOLD,
    ).verdicts[0].reason == "copy"
    backend.audit["result_private"]["worker_security_audit"]["reference"]["passed"] = False
    with pytest.raises(ValueError, match="incomplete or unsafe"):
        select_replay_final("week-1", backend=backend, gold_by_hotkey=GOLD)
