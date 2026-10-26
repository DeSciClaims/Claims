import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from competition.pilot_controller_factory import (
    _duplicate_links, _final_audit, _voters, create_controller_inputs,
)
from competition.duplicate_review import accepted_snapshot, make_duplicate_review
from competition.controller_inputs import build_provisional_evaluation_policy
from competition.code_audit import automatic_code_audit, finalize_code_audit
from competition.publication import canonical_sha256


class Backend:
    def get_round(self, round_id):
        policy = {"code_audit": self.audit_policy} if hasattr(self, "audit_policy") else {}
        return {"round_id": round_id, "network": "testnet", "rules_public": {"policy": policy}}

    def qualification_snapshot(self, round_id, hotkeys):
        return {"round_id": round_id, "hotkeys": [
            {"hotkey": key, "qualified": key != "unqualified"} for key in hotkeys
        ]}

    def list_submissions(self, _):
        return [{"hotkey": "miner-a", "team_id": "team-a"}]

    def list_stages(self, _):
        return [{"entries": [
            {"hotkey": "miner-a", "package_sha256": "a" * 64,
             "team_id": "team-a", "role": "challenger"},
            {"hotkey": "incumbent", "package_sha256": "b" * 64,
             "team_id": "king", "role": "incumbent"},
        ]}]


def test_factory_reads_post_lock_duplicate_review_and_checks_reveal(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_COMPETITION_ROUNDS_DIR", str(tmp_path))
    class ReviewBackend:
        record = {"round_id": "week1", "status": "locked",
                  "archive_block_hash": "0x" + "a" * 64,
                  "baseline_package_sha256": "9" * 64,
                  "configuration_sha256": "c" * 64,
                  "incumbent_hotkey": None}
        submissions = [{"hotkey": key, "team_id": key,
                        "package_sha256": digest * 64, "status": "accepted",
                        "qualified": True}
                       for key, digest in (("a", "1"), ("b", "2"))]

        def get_round(self, _):
            return self.record

        def list_submissions(self, _):
            return self.submissions

    backend = ReviewBackend()
    _, accepted_hash = accepted_snapshot(backend.submissions)
    candidate = {
        "schema_version": "claims_duplicate_candidates_v1",
        "round_id": "week1", "archive_block_hash": backend.record["archive_block_hash"],
        "baseline_package_sha256": backend.record["baseline_package_sha256"],
        "configuration_sha256": backend.record["configuration_sha256"],
        "accepted_submissions_sha256": accepted_hash,
        "incumbent_package_sha256": None,
        "public_base_package_sha256": [], "scan_errors": {},
        "comparisons": [{
            "left": "a", "right": "b", "left_package_sha256": "1" * 64,
            "right_package_sha256": "2" * 64,
            "structural_similarity": 0.5, "normalized_diff_fraction": 0.5,
            "shared_structural_nodes": 1, "total_structural_nodes": 2,
            "identical_nonbase_source_files": 0, "shared_resource_hashes": 0,
            "left_public_base_files_removed": 0, "right_public_base_files_removed": 0,
        }],
    }
    (tmp_path / "week1.duplicate-candidates.json").write_text(json.dumps(candidate))
    review = make_duplicate_review(
        "week1", round_record=backend.record, submissions=backend.submissions,
        linked_pairs=[("a", "b", "reviewed match")], candidate_report=candidate,
    )
    (tmp_path / "week1.duplicates.json").write_text(json.dumps(review))
    assert _duplicate_links(backend, "week1") == [("a", "b", "reviewed match")]
    backend.record.update(status="revealed", duplicate_review_links=review["links"],
                          duplicate_review_sha256=review["links_sha256"])
    assert _duplicate_links(backend, "week1") == [("a", "b", "reviewed match")]
    backend.record["duplicate_review_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="revealed sample"):
        _duplicate_links(backend, "week1")


def test_late_voter_and_code_audit_receipts_are_package_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_COMPETITION_ROUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("CLAIMS_COMPETITION_NETUID", "111")
    (tmp_path / "week1.voters.json").write_text(json.dumps({
        "round_id": "week1", "voters": [
            {"voter_hotkey": "miner-a", "team_id": "team-a"},
            {"voter_hotkey": "voter-b", "team_id": "team-b"},
        ],
    }))
    evidence = {"schema_version": "claims_final_code_audit_v1", "round_id": "week1",
                "stage": "final", "sample_sha256": "s" * 64,
                "packages": [{"hotkey": "miner-a", "package_sha256": "a" * 64,
                              "build_passed": True, "denied_egress_packets": 0}]}
    monkeypatch.setattr("competition.pilot_controller_factory.collect_final_code_evidence",
                        lambda *args, **kwargs: evidence)
    receipt = finalize_code_audit(evidence, {
        "round_id": "week1", "evidence_sha256": canonical_sha256(evidence),
        "reviewer": "claims-reviewer", "packages": [{
            "hotkey": "miner-a", "package_sha256": "a" * 64,
            "checks": {key: True for key in (
                "hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation",
            )},
        }],
    })
    (tmp_path / "week1.audit.json").write_text(json.dumps(receipt))
    backend = Backend()
    assert [row["voter_hotkey"] for row in _voters(backend, "week1")] == ["miner-a", "voter-b"]
    chain = SimpleNamespace(subtensor=SimpleNamespace(
        metagraph=lambda netuid: SimpleNamespace(hotkeys=[
            "owner", "operator", "miner-a", "incumbent",
        ]),
    ))
    audit = _final_audit(
        backend, chain, "week1", owner_hotkey="owner", operator_hotkey="operator",
        fetch_package=lambda job: b"package",
    )
    assert audit.code_passed_by_hotkey == {"miner-a": True}
    assert audit.code_failure_counts_by_hotkey == {"miner-a": 0}
    assert audit.code_evidence_sha256 == canonical_sha256(evidence)
    assert audit.qualified_by_hotkey == {"incumbent": True, "miner-a": True}
    assert audit.uid_by_hotkey["miner-a"] == 2
    assert audit.production_team_id == "king"
    receipt["packages"][0]["package_sha256"] = "c" * 64
    (tmp_path / "week1.audit.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="frozen package"):
        _final_audit(backend, chain, "week1", owner_hotkey="owner", operator_hotkey="operator",
                     fetch_package=lambda job: b"package")


def test_controller_recomputes_automatic_code_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_COMPETITION_ROUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("CLAIMS_COMPETITION_NETUID", "111")
    policy = {"min_completed_papers": 2, "max_repeated_output_fraction": 0.5,
              "max_cap_trip_fraction": 0.0, "max_claim_count_ratio": 3.0}
    evidence = {"schema_version": "claims_final_code_audit_v1", "round_id": "week1",
                "stage": "final", "sample_sha256": "s" * 64,
                "packages": [{
                    "hotkey": "miner-a", "package_sha256": "a" * 64,
                    "build_passed": True, "denied_egress_packets": 0,
                    "final_job_count": 2, "completed_job_count": 2,
                    "cap_trip_count": 0, "max_same_statement_set_papers": 1,
                    "completed_claim_count_median": 3, "baseline_claim_count_median": 2,
                    "source_indicators": {"embedded_sample_paper_id_count": 0,
                                          "embedded_sample_pdf_hash_count": 0,
                                          "evaluation_context_markers": []},
                }]}
    monkeypatch.setattr("competition.pilot_controller_factory.collect_final_code_evidence",
                        lambda *args, **kwargs: evidence)
    backend = Backend()
    backend.audit_policy = policy
    chain = SimpleNamespace(subtensor=SimpleNamespace(
        metagraph=lambda netuid: SimpleNamespace(hotkeys=[
            "owner", "operator", "miner-a", "incumbent",
        ]),
    ))
    audit = _final_audit(backend, chain, "week1", owner_hotkey="owner",
                         operator_hotkey="operator", fetch_package=lambda job: b"package")
    assert audit.code_passed_by_hotkey == {"miner-a": True}
    receipt = json.loads((tmp_path / "week1.audit.json").read_text())
    assert receipt == automatic_code_audit(evidence, policy)
    assert (tmp_path / "week1.audit-evidence.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "week1.audit.json").stat().st_mode & 0o777 == 0o600
    assert _final_audit(backend, chain, "week1", owner_hotkey="owner",
                        operator_hotkey="operator", fetch_package=lambda job: b"package") == audit
    receipt["packages"][0]["checks"]["hardcoded_output"] = False
    (tmp_path / "week1.audit.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="already differs"):
        _final_audit(backend, chain, "week1", owner_hotkey="owner",
                     operator_hotkey="operator", fetch_package=lambda job: b"package")


def test_controller_factory_rejects_runtime_that_differs_from_frozen_rules(monkeypatch):
    configuration = {"weekly_budget_usd": "750", "worker_policy": {"lease_seconds": 3600},
                     "decision_routing": {
                         "prescreen_confidence_threshold": 0.8,
                         "screen_confidence_threshold": 0.9,
                         "api_base": "https://api.openai.com/v1",
                     },
                     "judge_panels": {
                         judge: [{"api_base": "https://openrouter.ai/api/v1",
                                  "provider_slug": "pinned-provider"} for _ in range(2)]
                         for judge in ("J1", "J2")
                     },
                     "tiebreak_pass": {"api_base": "https://openrouter.ai/api/v1",
                                       "provider_slug": "pinned-provider"}}
    digest = canonical_sha256(configuration)
    document = {"round_id": "week1", "configuration": configuration,
                "configuration_sha256": digest,
                "baseline_upload": {"hotkey": "claims-reference",
                                    "package_sha256": "a" * 64}, "linked_pairs": []}
    monkeypatch.setattr("competition.pilot_controller_factory._round_file", lambda _: document)
    monkeypatch.setattr("competition.pilot_controller_factory.hippius_s3_client",
                        lambda **kwargs: object())
    fake_bt = ModuleType("bittensor")
    fake_bt.Subtensor = lambda network: SimpleNamespace(network=network)
    fake_bt.Wallet = lambda **kwargs: SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address="publisher"),
    )
    monkeypatch.setitem(sys.modules, "bittensor", fake_bt)
    monkeypatch.setattr("competition.pilot_controller_factory.build_provisional_evaluation_policy",
                        lambda **kwargs: SimpleNamespace(
                            round_id="week1", configuration_sha256=digest,
                            configuration=configuration,
                        ))
    settings = {
        "CLAIMS_ARCHIVE_SUBTENSOR_NETWORK": "archive",
        "CLAIMS_COMPETITION_WALLET_NAME": "wallet",
        "CLAIMS_COMPETITION_WALLET_HOTKEY": "hotkey",
        "CLAIMS_COMPETITION_WALLET_PATH": "/tmp/wallet",
        "CLAIMS_COMPETITION_PUBLISHER_HOTKEY": "publisher",
        "CLAIMS_COMPETITION_OPERATOR_HOTKEY": "operator",
        "CLAIMS_COMPETITION_OWNER_HOTKEY": "owner",
        "CLAIMS_COMPETITION_NETUID": "111",
        "CLAIMS_HIPPIUS_ENDPOINT": "https://s3.hippius.com",
        "CLAIMS_HIPPIUS_ACCESS_KEY_ID": "id",
        "CLAIMS_HIPPIUS_SECRET_ACCESS_KEY": "secret",
        "CLAIMS_HIPPIUS_PACKAGE_BUCKET": "packages",
        "CLAIMS_HIPPIUS_LOG_BUCKET": "logs",
        "CLAIMS_COMPETITION_DECISION_API_KEY": "decision-key",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    row = {"round_id": "week1", "network": "testnet",
           "configuration_sha256": digest, "baseline_package_sha256": "a" * 64,
           "reference_release_id": "bronze-1", "incumbent_hotkey": None}
    inputs = create_controller_inputs(row, object())
    assert inputs.evaluation.configuration_sha256 == digest
    assert inputs.operator_hotkey == "operator"
    document["configuration"] = {**configuration, "weekly_budget_usd": "1"}
    with pytest.raises(ValueError, match="frozen hash"):
        create_controller_inputs(row, object())


def test_controller_factory_reconstructs_real_provisional_judges(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "pilot-test-key")
    for key in ("CLAIMS_JUDGE_J1_A_PROVIDER", "CLAIMS_JUDGE_J1_B_PROVIDER",
                "CLAIMS_JUDGE_J2_A_PROVIDER", "CLAIMS_JUDGE_J2_B_PROVIDER",
                "CLAIMS_JUDGE_TIEBREAK_PROVIDER"):
        monkeypatch.setenv(key, "pinned-provider")
    worker_policy = {
        "allowed_models": ["qwen/qwen3-32b"],
        "published_token_ceiling": 100_000,
        "dollar_limit_usd": "1",
        "max_inference_output_tokens": 8192,
        "lease_seconds": 3600,
        "inference_base_url": "https://gateway.example/v1",
    }
    policy = build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(round_id="week1", source_context_for_paper=lambda _: {}),
        decision_api_key="pilot-decision-key", weekly_budget_usd=750,
        worker_policy=worker_policy,
    )
    document = {"round_id": "week1", "configuration_sha256": policy.configuration_sha256,
                "configuration": policy.configuration,
                "baseline_upload": {"hotkey": "claims-reference",
                                    "package_sha256": "a" * 64}, "linked_pairs": []}
    (tmp_path / "week1.json").write_text(json.dumps(document))
    monkeypatch.setenv("CLAIMS_COMPETITION_ROUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("CLAIMS_COMPETITION_DECISION_API_KEY", "pilot-decision-key")
    for key, value in {
        "CLAIMS_ARCHIVE_SUBTENSOR_NETWORK": "archive",
        "CLAIMS_COMPETITION_WALLET_NAME": "wallet",
        "CLAIMS_COMPETITION_WALLET_HOTKEY": "hotkey",
        "CLAIMS_COMPETITION_WALLET_PATH": str(tmp_path),
        "CLAIMS_COMPETITION_PUBLISHER_HOTKEY": "publisher",
        "CLAIMS_COMPETITION_OPERATOR_HOTKEY": "operator",
        "CLAIMS_COMPETITION_OWNER_HOTKEY": "owner",
        "CLAIMS_COMPETITION_NETUID": "111",
        "CLAIMS_HIPPIUS_ENDPOINT": "https://s3.hippius.com",
        "CLAIMS_HIPPIUS_ACCESS_KEY_ID": "id",
        "CLAIMS_HIPPIUS_SECRET_ACCESS_KEY": "secret",
        "CLAIMS_HIPPIUS_PACKAGE_BUCKET": "packages",
        "CLAIMS_HIPPIUS_LOG_BUCKET": "logs",
    }.items():
        monkeypatch.setenv(key, value)
    fake_bt = ModuleType("bittensor")
    fake_bt.Subtensor = lambda network: SimpleNamespace(network=network)
    fake_bt.Wallet = lambda **kwargs: SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address="publisher"),
    )
    monkeypatch.setitem(sys.modules, "bittensor", fake_bt)
    monkeypatch.setattr("competition.pilot_controller_factory.hippius_s3_client",
                        lambda **kwargs: object())
    row = {"round_id": "week1", "network": "testnet",
           "configuration_sha256": policy.configuration_sha256,
           "baseline_package_sha256": "a" * 64,
           "reference_release_id": "bronze-1", "incumbent_hotkey": None}
    inputs = create_controller_inputs(row, object())
    assert inputs.evaluation.configuration_sha256 == policy.configuration_sha256
    assert inputs.evaluation.public_judge_config() == policy.public_judge_config()
