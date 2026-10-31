import json
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from competition.publication import canonical_sha256
from competition.controller_inputs import build_provisional_evaluation_policy
from competition.round_setup import prepare_pilot_round, prepare_round, round_id_for_draft


def _draft():
    return {
        "round_id": "week1", "network": "testnet", "week_start": "2026-11-02",
        "deadline_at": datetime(2026, 11, 2, 12, tzinfo=UTC).isoformat(),
        "deadline_block": 100, "reveal_block": 700,
        "reference_release_id": "bronze-1", "incumbent_hotkey": None,
        "baseline_upload": {
            "hotkey": "claims-reference", "team_id": "claims",
            "package_sha256": "a" * 64,
            "package_object_key": "sealed/reference.whl",
            "package_size_bytes": 123,
            "manifest": {"entrypoint": "miner:main", "team_id": "claims"},
        },
        "configuration": {"pilot": True},
        "policy_public": {
            "min_final_papers": 500,
            "reference_lock_sha256": "f" * 64,
            "duplicate_verdict_public_at": "2026-11-03T09:00:00+00:00",
            "duplicate_dispute_window": {
                "opens_at": "2026-11-03T09:00:00+00:00",
                "closes_at": "2026-11-03T15:00:00+00:00",
            },
        },
    }


def test_round_id_defaults_to_utc_deadline_and_network(tmp_path):
    draft = _draft()
    draft.pop("round_id")
    expected = "round_20261102T120000Z_testnet"
    assert round_id_for_draft(draft) == expected
    same_instant = {**draft, "deadline_at": "2026-11-02T13:00:00+01:00"}
    assert round_id_for_draft(same_instant) == expected
    assert round_id_for_draft({**draft, "network": "mainnet"}).endswith("_mainnet")
    assert round_id_for_draft({**draft, "round_id": "manual_rehearsal"}) == "manual_rehearsal"
    with pytest.raises(ValueError, match="round ID"):
        round_id_for_draft({**draft, "round_id": "bad/id"})

    class Backend:
        def create_round(self, payload):
            return payload

    receipt = prepare_round(draft, backend=Backend(), directory=tmp_path)
    assert receipt["round_id"] == expected
    assert (tmp_path / f"{expected}.rules.json").is_file()
    assert prepare_round(same_instant, backend=Backend(), directory=tmp_path) == receipt
    with pytest.raises(ValueError, match="already differs"):
        prepare_round({**draft, "configuration": {"changed": True}},
                      backend=Backend(), directory=tmp_path)


def test_prepare_round_writes_private_and_public_artifacts_before_intake(tmp_path):
    class Backend:
        def create_round(self, payload):
            assert (tmp_path / "week1.json").is_file()
            assert (tmp_path / "week1.rules.json").is_file()
            assert payload["rules_public"] == json.loads((tmp_path / "week1.rules.json").read_text())
            return {**payload, "status": "open"}

    draft = _draft()
    result = prepare_round(draft, backend=Backend(), directory=tmp_path)
    rules_bytes = (tmp_path / "week1.rules.json").read_bytes()
    rules = json.loads(rules_bytes)
    assert rules["baseline_package_sha256"] == "a" * 64
    assert "package_object_key" not in rules_bytes.decode()
    assert result["rules_sha256"] == canonical_sha256(rules)
    assert (tmp_path / "week1.json").stat().st_mode & 0o777 == 0o600
    assert prepare_round(draft, backend=Backend(), directory=tmp_path) == result
    class TamperingBackend:
        def create_round(self, payload):
            return {**payload, "rules_public": {**payload["rules_public"], "policy": {}}}

    with pytest.raises(ValueError, match="did not preserve"):
        prepare_round(draft, backend=TamperingBackend(), directory=tmp_path)
    draft["configuration"]["pilot"] = False
    with pytest.raises(ValueError, match="already differs"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)


def test_prepare_round_rejects_invalid_calendar_before_writing(tmp_path):
    draft = _draft()
    draft["deadline_at"] = datetime(2026, 11, 6, 12, tzinfo=UTC).isoformat()
    with pytest.raises(ValueError, match="Monday 12:00"):
        prepare_round(draft, backend=object(), directory=tmp_path)
    draft = _draft()
    draft["week_start"] = "2026-11-06"
    with pytest.raises(ValueError, match="week start"):
        prepare_round(draft, backend=object(), directory=tmp_path)
    draft = _draft()
    draft["reveal_block"] = 699
    with pytest.raises(ValueError, match=r"D\+600"):
        prepare_round(draft, backend=object(), directory=tmp_path)
    draft = _draft()
    draft["linked_pairs"] = [["a", "b", "reviewed copy"]]
    with pytest.raises(ValueError, match="after the intake lock"):
        prepare_round(draft, backend=object(), directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_prepare_round_freezes_review_only_dispute_window(tmp_path):
    draft = _draft()

    class Backend:
        def create_round(self, payload):
            return payload

    prepare_round(draft, backend=Backend(), directory=tmp_path)
    rules = json.loads((tmp_path / "week1.rules.json").read_text())
    assert rules["policy"]["duplicate_dispute_window"] == draft["policy_public"]["duplicate_dispute_window"]
    assert rules["policy"]["duplicate_verdict_public_at"] == "2026-11-03T09:00:00+00:00"
    draft["policy_public"]["duplicate_dispute_window"]["opens_at"] = "2026-11-09T10:00:00+00:00"
    with pytest.raises(ValueError, match="Tuesday 09:00"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)
    draft = _draft()
    del draft["policy_public"]["duplicate_dispute_window"]
    with pytest.raises(ValueError, match="window is required"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)
    draft = _draft()
    draft["policy_public"]["duplicate_verdict_public_at"] = "2026-11-10T10:00:00+00:00"
    with pytest.raises(ValueError, match="Tuesday 09:00"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)
    draft = _draft()
    draft["policy_public"].pop("reference_lock_sha256")
    with pytest.raises(ValueError, match="Bronze reference lock"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)


def test_mainnet_round_requires_published_automatic_audit_thresholds(tmp_path):
    draft = _draft()
    draft["network"] = "mainnet"
    with pytest.raises(ValueError, match="automatic code audit"):
        prepare_round(draft, backend=object(), directory=tmp_path)
    draft["policy_public"]["code_audit"] = {
        "min_completed_papers": 200, "max_cap_trip_fraction": 0.01, "max_claim_count_ratio": 4.0,
    }

    class Backend:
        def create_round(self, payload):
            return payload

    prepare_round(draft, backend=Backend(), directory=tmp_path)
    rules = json.loads((tmp_path / "week1.rules.json").read_text())
    assert rules["policy"]["code_audit"] == draft["policy_public"]["code_audit"]


def test_pilot_setup_refuses_unfilled_round_template(tmp_path):
    from competition.round_setup import prepare_pilot_round

    draft = _draft()
    draft["reference_release_id"] = "REPLACE_REFERENCE_RELEASE_ID"
    with pytest.raises(ValueError, match="template placeholders"):
        prepare_pilot_round(
            draft, backend=object(), directory=tmp_path,
            storage=object(), decision_api_key="test-key",
        )


def test_prepare_round_accepts_accelerated_testnet_schedule(tmp_path):
    draft = _draft()
    draft["deadline_at"] = "2026-11-04T10:00:00+00:00"
    draft["reveal_block"] = 105
    draft["policy_public"].update({
        "schedule": {
            "mode": "custom", "reveal_delay_blocks": 5,
            "gold_close_at": "2026-11-04T11:00:00+00:00",
            "publish_at": "2026-11-04T11:15:00+00:00",
            "production_activate_at": "2026-11-04T11:30:00+00:00",
        },
        "duplicate_verdict_public_at": "2026-11-04T10:10:00+00:00",
        "duplicate_dispute_window": {
            "opens_at": "2026-11-04T10:10:00+00:00",
            "closes_at": "2026-11-04T10:20:00+00:00",
        },
    })

    class Backend:
        def create_round(self, payload):
            return payload

    prepare_round(draft, backend=Backend(), directory=tmp_path)
    rules = json.loads((tmp_path / "week1.rules.json").read_text())
    assert rules["policy"]["schedule"]["reveal_delay_blocks"] == 5
    draft["network"] = "mainnet"
    draft["policy_public"]["code_audit"] = {
        "min_completed_papers": 2, "max_cap_trip_fraction": 0.01, "max_claim_count_ratio": 4.0,
    }
    mainnet_dir = tmp_path / "mainnet"
    mainnet_dir.mkdir()
    prepare_round(draft, backend=Backend(), directory=mainnet_dir)
    assert json.loads((mainnet_dir / "week1.rules.json").read_text())["network"] == "mainnet"


def test_pilot_setup_checks_real_judges_and_sealed_baseline_before_intake(
    tmp_path, monkeypatch,
):
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
        decision_api_key="decision-key", weekly_budget_usd=750,
        worker_policy=worker_policy,
    )
    package = b"sealed reference wheel"
    draft = _draft()
    draft["configuration"] = policy.configuration
    draft["baseline_upload"]["package_sha256"] = hashlib.sha256(package).hexdigest()
    draft["baseline_upload"]["package_size_bytes"] = len(package)

    class Storage:
        def package_for_submission(self, upload):
            assert upload["package_sha256"] == hashlib.sha256(package).hexdigest()
            return package

    class Backend:
        def create_round(self, payload):
            assert payload["configuration_sha256"] == policy.configuration_sha256
            return payload

    receipt = prepare_pilot_round(
        draft, backend=Backend(), directory=tmp_path,
        storage=Storage(), decision_api_key="decision-key",
    )
    assert receipt["configuration_sha256"] == policy.configuration_sha256
    draft["configuration"] = {**policy.configuration, "judge_model_allowlist": ["altered"]}
    with pytest.raises(ValueError, match="runtime differs"):
        prepare_pilot_round(
            draft, backend=Backend(), directory=tmp_path,
            storage=Storage(), decision_api_key="decision-key",
        )


def test_pilot_setup_generates_frozen_configuration_from_json_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "pilot-test-key")
    worker_policy = {
        "allowed_models": ["qwen/qwen3-32b"],
        "published_token_ceiling": 100_000,
        "dollar_limit_usd": "1",
        "max_inference_output_tokens": 8192,
        "lease_seconds": 3600,
        "inference_base_url": "https://gateway.example/v1",
    }
    judge_specs = {
        "api_base": "https://openrouter.ai/api/v1", "temperature": 1.0,
        "max_tokens": 8192, "timeout_seconds": 180,
        "max_in_flight": 16,
        "J1": [{"model": "qwen/qwen3-32b", "provider_slug": "provider-a"},
               {"model": "openai/gpt-oss-120b", "provider_slug": "provider-b"}],
        "J2": [{"model": "deepseek/deepseek-v3.2", "provider_slug": "provider-c"},
               {"model": "meta-llama/llama-4-maverick", "provider_slug": "provider-d"}],
        "tiebreak": {"model": "qwen/qwen3-235b-a22b-2507",
                     "provider_slug": "provider-e"},
    }
    draft = _draft()
    draft.pop("round_id")
    draft.pop("configuration")
    draft["configuration_spec"] = {
        "workflow_specs": {
            "comparison_model_ref": "J1.0", "canonicalization_model_ref": "tiebreak",
            "canonical_audit_model_ref": "tiebreak", "comparison_harness": "dspy",
            "canonicalization_harness": "dspy", "eligibility_output_contract": "compact",
            "max_tokens": 32768, "timeout_seconds": 1800, "equivalence_group_bypass": True,
        },
        "judge_specs": judge_specs,
        "decision_routing": {
            "provider": "jev", "model": "jev-latest",
            "api_base": "https://api.typesafe.ai/v1",
            "prescreen_acceptance_fraction": 0.8,
            "screen_confidence_threshold": 0.9,
        },
        "worker_policy": worker_policy, "weekly_budget_usd": 10,
    }
    class Storage:
        def package_for_submission(self, _):
            return b"sealed"
    class Backend:
        def create_round(self, payload):
            return payload
    prepare_pilot_round(draft, backend=Backend(), directory=tmp_path,
                        storage=Storage(), decision_api_key="decision-key")
    rules = json.loads((tmp_path / "round_20261102T120000Z_testnet.rules.json").read_text())
    assert rules["round_id"] == "round_20261102T120000Z_testnet"
    assert rules["configuration"]["decision_routing"]["provider"] == "jev"
    assert rules["configuration"]["judge_specs"] == judge_specs
    assert rules["configuration"]["judge_panels"]["J1"][0]["model"] == "qwen/qwen3-32b"
    assert "decision-key" not in (tmp_path / "round_20261102T120000Z_testnet.rules.json").read_text()
