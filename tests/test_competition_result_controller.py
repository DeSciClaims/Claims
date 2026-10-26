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
            "reference_package_sha256": "2" * 64,
            "incumbent_hotkey": "incumbent",
        }

    def list_stages(self, _round_id):
        return [{"stage": "final", "entries": [
            {"hotkey": "incumbent", "role": "incumbent", "team_id": "old-team",
             "package_sha256": "1" * 64},
            {"hotkey": "winner", "role": "challenger", "team_id": "new-team"},
            {"hotkey": "runner", "role": "challenger", "team_id": "runner-team"},
        ]}]

    def list_sample(self, _round_id):
        return [
            {"stage": "final", "paper_id": "paper-1", "judge_id": "J1"},
            {"stage": "final", "paper_id": "paper-2", "judge_id": "J2"},
        ]

    def worker_inference_cost_summary(self, round_id):
        return {"round_id": round_id, "scope": "completed_worker_inference",
                "by_stage": [{"stage": "final", "job_count": 6, "usd": "1.20000000"}],
                "total_jobs": 6, "total_usd": "1.20000000"}

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
        "stage": "final", "paper_id": paper_id, "judge_id": judge_id,
        "challenger_hotkey": hotkey,
        "baseline_hotkey": "incumbent", "challenger_score": score,
        "baseline_score": 0.5, "infrastructure_fault": False,
    } for paper_id, judge_id in (("paper-1", "J1"), ("paper-2", "J2"))
      for hotkey, score in (("winner", 0.54), ("runner", 0.52))))
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
        copy_audit={
            "winner": {"common_papers": 500, "jaccard": 0.4, "mean_gain": 0.04,
                       "standard_error": 0.005, "is_copy": False},
            "runner": {"common_papers": 500, "jaccard": 0.9, "mean_gain": 0.02,
                       "standard_error": 0.005, "is_copy": False},
        },
        worker_security_audit={
            "incumbent": {"paper_count": 500, "denied_papers": 0,
                          "denied_packets": 0, "passed": True},
            "winner": {"paper_count": 500, "denied_papers": 0,
                       "denied_packets": 0, "passed": True},
            "runner": {"paper_count": 500, "denied_papers": 0,
                       "denied_packets": 0, "passed": True},
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
    assert len(bundle["results"]["final_pair_scores"]) == 4
    assert bundle["results"]["judge_diagnostics"]["winner"]["J1"]["paper_count"] == 1
    assert bundle["results"]["worker_inference_cost"]["total_usd"] == "1.20000000"
    assert bundle["results"]["copy_audit"]["winner"]["jaccard"] == 0.4
    assert bundle["results"]["worker_security_audit"]["winner"]["passed"] is True
    assert {row["uid"]: row["weight"] for row in bundle["weights"]["vector"]} == {
        0: 0.01, 1: 0.07, 2: 0.02, 9: 0.90,
    }


def test_signed_reference_fallback_burns_all_miner_shares():
    class ReferenceBackend(Backend):
        def list_stages(self, _round_id):
            return [
                {"stage": "final", "entries": []},
                {"stage": "rerun", "source_stage": "final", "entries": [
                    {"hotkey": "reference", "role": "reference", "team_id": "claims",
                     "package_sha256": "2" * 64},
                    {"hotkey": "near", "role": "challenger", "team_id": "other-team"},
                ]},
            ]

        def list_sample(self, _round_id):
            return [
                {"stage": "rerun", "paper_id": "paper-1", "judge_id": "J1"},
                {"stage": "rerun", "paper_id": "paper-2", "judge_id": "J2"},
            ]

    backend = ReferenceBackend()
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    selection = FinalSelection(
        None, "reference_fallback", 0.0,
        (FinalistVerdict("near", "other-team", 0.02, 0.05, 2, False,
                         "below_crown_bar"),),
        final_pair_rows=tuple({
            "stage": "rerun", "paper_id": paper_id, "judge_id": judge_id,
            "challenger_hotkey": "near", "baseline_hotkey": "reference",
            "baseline_package_sha256": "2" * 64,
            "challenger_score": 0.52, "baseline_score": 0.5,
            "infrastructure_fault": False,
        } for paper_id, judge_id in (("paper-1", "J1"), ("paper-2", "J2"))),
    )
    receipt = publish_selected_round(
        "week2", backend=backend, signer=signer, selection=selection,
        operator_hotkey="operator", uid_by_hotkey={"operator": 9, "near": 1},
        qualified_by_hotkey={"near": True}, production_team_id="old-team",
        gold_by_hotkey={"near": GoldResult(60, 60, 10, 1 / 6, False)},
        copy_audit={"near": {"is_copy": False}},
        worker_security_audit={
            "reference": {"passed": True}, "near": {"passed": True},
        },
        gold_check={"seed": "c" * 64, "candidate_ids": [], "case_ids": []},
    )
    bundle = receipt["bundle"]
    assert backend.published == ("week2", receipt)
    assert bundle["results"]["decision"] == "reference_fallback"
    assert bundle["results"]["fallback_reference_hotkey"] == "reference"
    assert bundle["results"]["production_hotkey"] is None
    assert bundle["weights"]["vector"] == [
        {"uid": 0, "weight": 0.1}, {"uid": 9, "weight": 0.9},
    ]
