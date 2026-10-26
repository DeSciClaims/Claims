"""Independent final checks consume local jobs and Silver receipts."""

import hashlib

import pytest

from competition.replay_final_audit import audit_replay_final
from competition.replay_store import ReplayStore
from test_competition_code_audit import Backend as CodeBackend, _auto_policy, _wheel
from test_competition_replay_store import _inputs


class FinalBackend(CodeBackend):
    def __init__(self, package_hash):
        super().__init__(package_hash)
        self.frozen = None

    def list_sample(self, _):
        return [{"stage": "final", "paper_id": paper_id,
                 "source_sha256": "e" * 64}
                for paper_id in ("paper-1", "paper-2")]

    def list_stage_pairs(self, _, stage):
        assert stage == "final"
        return [{
            "stage": stage, "paper_id": paper_id,
            "challenger_hotkey": "miner", "baseline_hotkey": "baseline",
            "challenger_package_sha256": self.package_hash,
            "baseline_package_sha256": "b" * 64,
            "challenger_score": 0.6, "baseline_score": 0.5,
            "infrastructure_fault": False,
        } for paper_id in ("paper-1", "paper-2")]

    def get_paper_evaluation(self, _, stage, paper_id):
        assert stage == "final"
        return {"input_sha256": "f" * 64, "result_private": {"silver": {
            "silver_record": {"paper_id": paper_id, "silver_units": [{
                "silver_unit_id": "shared", "paper_id": paper_id,
                "required_for_completeness": True, "scoring_mode": "required",
                "equivalent_candidate_ids": ["baseline-claim", "miner-claim"],
            }]},
            "miner_submissions": [
                {"miner_id": "baseline", "candidates": [{"candidate_id": "baseline-claim"}]},
                {"miner_id": "miner", "candidates": [{"candidate_id": "miner-claim"}]},
            ],
        }}}

    def freeze_final_audit(self, round_id, *, input_sha256, result_private):
        result = {"input_sha256": input_sha256, "result_private": result_private}
        if self.frozen is not None and self.frozen != result:
            raise ValueError("replay final audit was already frozen differently")
        self.frozen = result
        return result


def test_replay_final_audit_uses_own_jobs_silver_and_frozen_policy():
    package = _wheel()
    backend = FinalBackend(hashlib.sha256(package).hexdigest())
    first = audit_replay_final(
        "week1", backend=backend, fetch_package=lambda _: package,
        code_policy=_auto_policy(),
    )
    result = first["result_private"]
    assert result["code_passed"] == {"miner": True}
    assert result["copy_audit"]["miner"]["is_copy"] is False
    assert result["worker_security_audit"]["baseline"]["passed"] is True
    assert audit_replay_final(
        "week1", backend=backend, fetch_package=lambda _: package,
        code_policy=_auto_policy(),
    ) == first
    with pytest.raises(ValueError, match="already frozen differently"):
        audit_replay_final(
            "week1", backend=backend, fetch_package=lambda _: package,
            code_policy={**_auto_policy(), "max_repeated_output_fraction": 0.5},
        )


def test_replay_store_freezes_private_final_audit_across_restart(tmp_path):
    snapshot, plan = _inputs()
    path = tmp_path / "replay.sqlite"
    store = ReplayStore(path)
    store.initialize(snapshot, plan)
    result = {"round_id": "round-1", "code_passed": {"miner": False}}
    store.freeze_final_audit("round-1", input_sha256="a" * 64,
                             result_private=result)
    restarted = ReplayStore(path)
    assert restarted.get_final_audit("round-1") == {
        "input_sha256": "a" * 64, "result_private": result,
    }
    with pytest.raises(ValueError, match="already frozen differently"):
        restarted.freeze_final_audit("round-1", input_sha256="b" * 64,
                                     result_private=result)
