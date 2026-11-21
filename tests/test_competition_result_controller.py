from bittensor_wallet import Keypair
import pytest

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
            "duplicate_review_sha256": canonical_sha256([]),
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

    def list_duplicate_disputes(self, _round_id):
        return []

    def worker_inference_cost_summary(self, round_id):
        return {"round_id": round_id, "scope": "reported_worker_attempt_inference",
                "by_stage": [{"stage": "final", "job_count": 6,
                              "reported_attempt_count": 6,
                              "unreported_attempt_count": 0, "usd": "1.20000000"}],
                "total_jobs": 6, "total_reported_attempts": 6,
                "total_unreported_attempts": 0, "total_usd": "1.20000000"}

    def worker_cost_ledger(self, round_id):
        return {"round_id": round_id,
                "scope": "completed_worker_jobs_and_frozen_paper_evaluations",
                "measured": {"worker_inference_usd": "1.20000000", "job_count": 6,
                             "reported_attempt_count": 6,
                             "unreported_attempt_count": 0,
                             "judge_inference_usd": "0.05000000", "judge_call_count": 2},
                "estimated": {"worker_vm_usd": "0.06000000", "attempt_count": 6,
                              "missing_attempt_count": 0,
                              "judge_inference_usd": "0.00000000",
                              "judge_call_count": 0},
                "judge_usage": {"frozen_paper_count": 2, "missing_paper_count": 0,
                                "unpriced_call_count": 0},
                "unreported_categories": ["decision_api", "storage", "expired_worker_attempts",
                                          "failed_judge_attempts", "gold_tiebreak_judging"],
                "completeness": "partial"}

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
        code_audit={"evidence_sha256": "a" * 64,
                    "failed_checks": {"winner": 0, "runner": 0}},
        gold_check={"seed": "c" * 64, "candidate_ids": [], "case_ids": []},
    )
    assert backend.published == ("week2", receipt)
    assert receipt["bundle_sha256"] == canonical_sha256(receipt["bundle"])
    assert Keypair(ss58_address=signer.ss58_address).verify(
        bytes.fromhex(receipt["bundle_sha256"]), bytes.fromhex(receipt["signature_hex"]),
    )
    bundle = receipt["bundle"]
    assert bundle["results"]["gold"]["winner"]["refusal"] is False
    assert bundle["results"]["duplicate_disputes"] == []
    assert bundle["results"]["gold_check"]["seed"] == "c" * 64
    assert len(bundle["results"]["final_pair_scores"]) == 4
    assert bundle["results"]["judge_diagnostics"]["winner"]["J1"]["paper_count"] == 1
    assert bundle["results"]["worker_inference_cost"]["total_usd"] == "1.20000000"
    assert bundle["results"]["cost_ledger"]["estimated"]["worker_vm_usd"] == "0.06000000"
    assert bundle["results"]["copy_audit"]["winner"]["jaccard"] == 0.4
    assert bundle["results"]["worker_security_audit"]["winner"]["passed"] is True
    assert bundle["results"]["code_audit"]["failed_checks"] == {"winner": 0, "runner": 0}
    assert {row["uid"]: row["weight"] for row in bundle["weights"]["vector"]} == pytest.approx({
        0: 0.875758 + 0.1 * (1 - 0.875758),
        1: 0.7 * (1 - 0.875758), 2: 0.2 * (1 - 0.875758),
    })


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
        code_audit={"evidence_sha256": "a" * 64,
                    "failed_checks": {"near": 0}},
        gold_check={"seed": "c" * 64, "candidate_ids": [], "case_ids": []},
    )
    bundle = receipt["bundle"]
    assert backend.published == ("week2", receipt)
    assert bundle["results"]["decision"] == "reference_fallback"
    assert bundle["results"]["fallback_reference_hotkey"] == "reference"
    assert bundle["results"]["production_hotkey"] is None
    assert bundle["weights"]["vector"] == [
        {"uid": 0, "weight": pytest.approx(1.0)},
    ]


@pytest.mark.parametrize('standard_error',[.005,float('inf')])
def test_scientific_single_paper_publication_cites_source_tasks_not_legacy_panels(monkeypatch,standard_error):
    from competition import scientific_lineage,scientific_scoring,result_controller
    class ScientificBackend(Backend):
        def get_round(self,rid):
            return {**super().get_round(rid),'status':'final','rules_public':{'policy':{'validation_protocol':'claims-source-task-v1','winner_selection':'silver_tiebreak'}}}
        def list_stages(self,rid):
            stage=super().list_stages(rid)[0]
            return [{**stage,'entries':[e for e in stage['entries'] if e['hotkey']!='runner']}]
        def list_sample(self,rid):return [{'stage':'final','paper_id':'paper-1','judge_id':'J2'}]
    backend=ScientificBackend();proof=[{'paper_id':'paper-1','silver_receipt_sha256':'a'*64,'revision':0,'comparison_plan_sha256':'b'*64}]
    copies={'winner':{'schema_version':'claims_source_copy_diagnostic_v1','is_copy':False}}
    monkeypatch.setattr(scientific_lineage,'verify_current_grid',lambda *a,**k:proof)
    monkeypatch.setattr(scientific_scoring,'scientific_copy_diagnostics',lambda **k:copies)
    monkeypatch.setattr(result_controller,'final_judge_diagnostics',lambda **k:(_ for _ in ()).throw(AssertionError('legacy panels called')))
    pair={'stage':'final','paper_id':'paper-1','judge_id':'J2','challenger_hotkey':'winner','baseline_hotkey':'incumbent','challenger_score':.54,'baseline_score':.5,'infrastructure_fault':False}
    selection=FinalSelection('winner','new_crown',.04,(FinalistVerdict('winner','new-team',.04,standard_error,1,True,'cleared'),),final_pair_rows=(pair,))
    signer=Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    receipt=publish_selected_round('week2',backend=backend,signer=signer,selection=selection,operator_hotkey='operator',uid_by_hotkey={'operator':9,'winner':1,'incumbent':3},qualified_by_hotkey={'winner':True},production_team_id='old-team',gold_by_hotkey={},copy_audit=copies,worker_security_audit={'winner':{'passed':True},'incumbent':{'passed':True}},code_audit={'evidence_sha256':'a'*64,'failed_checks':{'winner':0}})
    assert receipt['bundle']['results']['judge_diagnostics']=={'protocol_id':'claims-source-task-v1','source_task_receipts':proof}
    assert backend.published==('week2',receipt)

    import json,math
    from pydantic import TypeAdapter
    assert receipt['bundle']['results']['finalists'][0]['standard_error']==(standard_error if math.isfinite(standard_error) else None)
    wire=json.dumps(receipt,allow_nan=False).encode()
    decoded=TypeAdapter(dict).validate_json(wire)
    assert canonical_sha256(decoded['bundle'])==receipt['bundle_sha256']
