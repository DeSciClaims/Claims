"""Frozen independent-replay reads reject changed rules and package links."""

import io
import json

import pytest

from competition.publication import canonical_sha256
from competition.replay_inputs import CompetitionReplayInputs, validate_replay_snapshot


def _snapshot():
    rules = {"configuration": {"worker_policy": {"allowed_models": ["model-a"]}}}
    return {
        "schema_version": "claims_competition_replay_snapshot_v1",
        "round_id": "week-1", "network": "testnet",
        "rules_public": rules, "rules_sha256": canonical_sha256(rules),
        "configuration_sha256": canonical_sha256(rules["configuration"]),
        "paper_pool_sha256": "a" * 64,
        "sample": [{"stage": "week_one", "paper_id": "paper-1",
                    "source_sha256": "b" * 64}],
        "stages": [{"stage": "week_one", "entries": [
            {"role": "challenger", "hotkey": "miner-1", "package_sha256": "c" * 64},
            {"role": "reference", "hotkey": "reference", "package_sha256": "d" * 64},
        ]}],
        "uploads": [{"hotkey": "miner-1", "package_sha256": "c" * 64,
                     "package_object_key": "sealed/miner-1", "package_size_bytes": 100,
                     "manifest": {"entrypoint": "miner:main"},
                     "salt_hex": "e" * 64, "commitment": "f" * 64,
                     "commit_block": 123, "status": "accepted"}],
    }


def test_replay_client_uses_get_only_and_validates_frozen_inputs():
    requests = []

    def respond(request, *, timeout):
        requests.append((request, timeout))
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer replay-only"
        return io.BytesIO(json.dumps(_snapshot()).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.snapshot("week-1")["uploads"][0]["package_object_key"] == "sealed/miner-1"
    assert requests[0][0].full_url.endswith("/replay/rounds/week-1/snapshot")
    assert requests[0][1] == 30


def test_replay_snapshot_rejects_changed_rule_and_missing_package():
    snapshot = _snapshot()
    snapshot["rules_public"]["configuration"]["worker_policy"]["allowed_models"] = ["changed"]
    with pytest.raises(ValueError, match="frozen round rules"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["uploads"] = []
    with pytest.raises(ValueError, match="lacks a selected sealed package"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["sample"].append(dict(snapshot["sample"][0]))
    with pytest.raises(ValueError, match="repeats a paper"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["uploads"][0]["status"] = "duplicate"
    with pytest.raises(ValueError, match="excluded duplicate"):
        validate_replay_snapshot(snapshot, "week-1")


def test_replay_client_rejects_unprotected_backend_url():
    with pytest.raises(ValueError, match="private HTTPS"):
        CompetitionReplayInputs("http://backend.example", "replay-only")
