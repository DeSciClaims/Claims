"""Independent signed weights bind the second validator's own score inputs."""

from copy import deepcopy

import pytest
from bittensor_wallet import Keypair

from competition.final_selection import FinalSelection, FinalistVerdict
from competition.replay_publication import (
    replay_publication_bundle, sign_replay_publication, verify_replay_publication,
)
from competition.weights import WeightPlan


class LocalResult:
    snapshot = {
        "round_id": "week-1", "network": "testnet",
        "rules_sha256": "a" * 64, "configuration_sha256": "b" * 64,
        "archive_block_hash": "0x" + "c" * 64,
        "drand_round": 42, "drand_seed": "d" * 64,
        "paper_pool_sha256": "e" * 64, "duplicate_review_sha256": "f" * 64,
        "baseline_package_sha256": "1" * 64,
        "reference_package_sha256": "1" * 64,
    }

    def __init__(self):
        self.pairs = [{"paper_id": "paper-1", "challenger_hotkey": "miner-1",
                       "challenger_score": 0.7, "baseline_score": 0.5}]

    def list_stages(self, round_id):
        return [{"stage": "week_one"}]

    def list_stage_pairs(self, round_id, stage):
        return self.pairs

    def list_sample(self, round_id):
        return [{"stage": "week_one", "paper_id": "paper-1"}]

    def get_final_audit(self, round_id):
        return {"input_sha256": "2" * 64,
                "result_private": {"round_id": round_id, "stage": "week_one"}}


def _selection(backend):
    return FinalSelection(
        "miner-1", "new_crown", 0.2,
        (FinalistVerdict("miner-1", "team-1", 0.2, 0.01, 500,
                         True, "cleared"),), tuple(backend.pairs),
    )


def test_signed_replay_receipt_binds_local_grid_and_explicit_gold_policy():
    backend = LocalResult()
    weights = WeightPlan(
        {0: 0.03, 1: 0.9, 2: 0.07},
        (("operator", "operator", 0.9), ("miner-1", "production", 0.07),
         ("", "runner_up_1_burned", 0.02), ("", "runner_up_2_burned", 0.01)),
        0.03,
    )
    bundle = replay_publication_bundle(
        "week-1", backend=backend, selection=_selection(backend),
        weight_plan=weights, gold_policy_id="independent-gold-v1",
        gold_evidence_sha256="3" * 64, qualification_evidence_sha256="4" * 64,
    )
    assert bundle["local_pairs_sha256"] != bundle["local_sample_sha256"]
    assert bundle["weights"]["vector"][2] == {"uid": 2, "weight": 0.07}
    assert bundle["gold_policy_id"] == "independent-gold-v1"
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    receipt = sign_replay_publication(bundle, signer)
    assert verify_replay_publication(
        receipt, verify_signature=lambda hotkey, message, signature:
        Keypair(ss58_address=hotkey).verify(message, signature),
    ) == bundle
    changed = deepcopy(receipt)
    changed["bundle"]["results"]["winner_gain"] = 0.3
    with pytest.raises(ValueError, match="signed bundle"):
        verify_replay_publication(changed, verify_signature=lambda *_: True)
    backend.pairs[0]["challenger_score"] = 0.8
    with pytest.raises(ValueError, match="local final score grid"):
        replay_publication_bundle(
            "week-1", backend=backend, selection=_selection(LocalResult()),
            weight_plan=weights, gold_policy_id="independent-gold-v1",
            gold_evidence_sha256="3" * 64, qualification_evidence_sha256="4" * 64,
        )


def test_replay_publication_requires_gold_and_qualification_evidence():
    backend = LocalResult()
    with pytest.raises(ValueError, match="Gold or qualification evidence"):
        replay_publication_bundle(
            "week-1", backend=backend, selection=_selection(backend),
            weight_plan=WeightPlan({1: 1.0}, (("operator", "operator", 1.0),), 0.0),
            gold_policy_id="independent-gold-v1",
            gold_evidence_sha256="", qualification_evidence_sha256="4" * 64,
        )
