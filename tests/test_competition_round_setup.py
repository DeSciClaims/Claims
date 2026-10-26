import json
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from competition.publication import canonical_sha256
from competition.controller_inputs import build_provisional_evaluation_policy
from competition.round_setup import prepare_pilot_round, prepare_round


def _draft():
    return {
        "round_id": "week1", "network": "testnet", "week_start": "2026-11-09",
        "deadline_at": datetime(2026, 11, 9, 12, tzinfo=UTC).isoformat(),
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
            "duplicate_dispute_window": {
                "opens_at": "2026-11-10T09:00:00+00:00",
                "closes_at": "2026-11-10T15:00:00+00:00",
            },
        },
    }


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
    draft["policy_public"]["duplicate_dispute_window"]["opens_at"] = "2026-11-09T10:00:00+00:00"
    with pytest.raises(ValueError, match="must follow the deadline"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)
    draft = _draft()
    del draft["policy_public"]["duplicate_dispute_window"]
    with pytest.raises(ValueError, match="window is required"):
        prepare_round(draft, backend=Backend(), directory=tmp_path)


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
