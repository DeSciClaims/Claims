"""Frozen independent-replay reads reject changed rules and package links."""

import io
import hashlib
import json

import pytest

from competition.publication import canonical_sha256
from competition.drand import VerifiedBeacon
from competition.replay_inputs import (
    CompetitionReplayInputs, validate_replay_snapshot, verify_replay_sample,
)
from competition.sample_release import PaperRecord, frozen_pool_hash, sample_receipt


def _snapshot():
    rules = {"configuration": {"worker_policy": {"allowed_models": ["model-a"]}}}
    return {
        "schema_version": "claims_competition_replay_snapshot_v1",
        "round_id": "week-1", "network": "testnet",
        "rules_public": rules, "rules_sha256": canonical_sha256(rules),
        "configuration_sha256": canonical_sha256(rules["configuration"]),
        "paper_pool_sha256": "a" * 64,
        "duplicate_review_links": [],
        "duplicate_review_sha256": canonical_sha256([]),
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


def test_replay_client_checks_entire_released_pool_hash():
    papers = [PaperRecord("paper-1", "a" * 64), PaperRecord("paper-2", "b" * 64)]
    digest = frozen_pool_hash(papers)
    body = {
        "round_id": "week-1", "reference_release_id": "reference-v1",
        "paper_pool_sha256": digest,
        "papers": [{"paper_id": row.paper_id, "source_sha256": row.source_sha256}
                   for row in papers],
    }
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        assert request.get_method() == "GET" and timeout == 30
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.paper_pool(
        "week-1", reference_release_id="reference-v1", expected_sha256=digest,
    ) == papers
    assert requests == [
        "https://backend.example/internal/competition/replay/rounds/week-1/paper-pool",
    ]
    body["papers"][0]["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="frozen hash"):
        client.paper_pool(
            "week-1", reference_release_id="reference-v1", expected_sha256=digest,
        )


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
    snapshot["duplicate_review_links"].append({"left": "a", "right": "b", "reason": "copy"})
    with pytest.raises(ValueError, match="frozen round rules"):
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


def test_replay_sample_accepts_backend_stage_order_and_rejects_changed_position():
    signature = b"x" * 96
    beacon = VerifiedBeacon(
        42, hashlib.sha256(signature).digest(), signature.hex(),
        "a" * 64, "b" * 96, 123456,
    )
    pool = [PaperRecord(f"paper-{index}", format(index + 1, "064x"))
            for index in range(3)]
    draw = sample_receipt(
        papers=pool, beacon=beacon, prescreen_count=1,
        screen_count=1, final_count=1,
    )
    snapshot = {
        "deadline_block": 100, "reveal_block": 700,
        **{key: value for key, value in draw.items() if key != "papers"},
        "sample": sorted(draw["papers"], key=lambda row: (row["stage"], row["position"])),
        "stages": [],
    }
    verify_replay_sample(snapshot, paper_pool=pool, verified_beacon=beacon)
    snapshot["sample"][0]["position"] += 1
    with pytest.raises(ValueError, match="independently redrawn"):
        verify_replay_sample(snapshot, paper_pool=pool, verified_beacon=beacon)
