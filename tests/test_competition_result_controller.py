from bittensor_wallet import Keypair

from competition.final_selection import FinalSelection, FinalistVerdict
from competition.gold import GoldResult
from competition.publication import canonical_sha256
from competition.result_controller import publish_selected_round


class Backend:
    def __init__(self):
        self.published = None

    def get_round(self, round_id):
        return {
            "round_id": round_id, "network": "testnet", "status": "gold_closed",
            "archive_block_hash": "0x" + "a" * 64,
            "drand_round": 10, "drand_seed": "b" * 64,
            "drand_chain_hash": "f" * 64, "drand_public_key": "a" * 96,
            "drand_signature": "b" * 192, "drand_scheduled_at": 1_800_000_000,
            "paper_pool_sha256": "c" * 64, "rules_sha256": "d" * 64,
            "configuration_sha256": "e" * 64,
            "baseline_package_sha256": "1" * 64,
            "incumbent_hotkey": "incumbent",
        }

    def list_stages(self, _round_id):
        return [{"stage": "final", "entries": [
            {"hotkey": "incumbent", "role": "incumbent", "team_id": "old-team"},
            {"hotkey": "winner", "role": "challenger", "team_id": "new-team"},
            {"hotkey": "runner", "role": "challenger", "team_id": "runner-team"},
        ]}]

    def publish_round(self, round_id, receipt):
        self.published = (round_id, receipt)
        return {"status": "published"}


def test_signed_publication_contains_crown_gold_and_70_20_10_weights():
    backend = Backend()
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    selection = FinalSelection("winner", "new_crown", 0.04, (
        FinalistVerdict("winner", "new-team", 0.04, 0.005, 500, True, "cleared"),
        FinalistVerdict("runner", "runner-team", 0.02, 0.005, 500, True, "cleared"),
    ), final_pair_rows=tuple({
        "stage": "final", "paper_id": "paper-1", "challenger_hotkey": hotkey,
        "baseline_hotkey": "incumbent", "challenger_score": score,
        "baseline_score": 0.5, "infrastructure_fault": False,
    } for hotkey, score in (("winner", 0.54), ("runner", 0.52))))
    receipt = publish_selected_round(
        "week2", backend=backend, signer=signer, selection=selection,
        operator_hotkey="operator", uid_by_hotkey={
            "operator": 9, "winner": 1, "runner": 2, "incumbent": 3,
        }, qualified_by_hotkey={"winner": True, "runner": True},
        production_team_id="old-team",
        gold_by_hotkey={
            "winner": GoldResult(60, 60, 10, 1 / 6, False),
            "runner": GoldResult(60, 60, 20, 1 / 3, False),
        },
        gold_check={"seed": "c" * 64, "candidate_ids": [], "case_ids": []},
    )
    assert backend.published == ("week2", receipt)
    assert receipt["bundle_sha256"] == canonical_sha256(receipt["bundle"])
    assert Keypair(ss58_address=signer.ss58_address).verify(
        bytes.fromhex(receipt["bundle_sha256"]), bytes.fromhex(receipt["signature_hex"]),
    )
    bundle = receipt["bundle"]
    assert bundle["results"]["gold"]["winner"]["refusal"] is False
    assert bundle["results"]["gold_check"]["seed"] == "c" * 64
    assert len(bundle["results"]["final_pair_scores"]) == 2
    assert {row["uid"]: row["weight"] for row in bundle["weights"]["vector"]} == {
        0: 0.01, 1: 0.07, 2: 0.02, 9: 0.90,
    }
