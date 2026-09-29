from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from neurons.consensus_validator import (
    ClaimsConsensusValidator,
    _confirm_source_failure,
    _preflight_consensus_sources,
    _seconds_until_deadline,
    _source_failure_payload,
    _subtensor_network_arg,
    _sync_metagraph,
)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--subtensor.network", "finney"], "finney"),
        (["--subtensor.network=finney"], "finney"),
        (["--subtensor.network", "test"], "test"),
        (["--subtensor.chain_endpoint", "wss://rpc.example.test"], "wss://rpc.example.test"),
        (["--subtensor.chain_endpoint=wss://rpc.example.test"], "wss://rpc.example.test"),
        (["--subtensor.network", "finney", "--subtensor.chain_endpoint", "wss://rpc.example.test"], "wss://rpc.example.test"),
        ([], None),
    ],
)
def test_consensus_subtensor_network_uses_dotted_cli_arguments(monkeypatch, args, expected) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subtensor.network", default="finney")
    parser.add_argument("--subtensor.chain_endpoint", default="wss://default.example.test")
    parsed = parser.parse_args(args)
    monkeypatch.setattr(sys, "argv", ["consensus_validator", *args])

    assert _subtensor_network_arg(parsed) == expected


class _Subtensor:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[tuple[int, bool]] = []

    def metagraph(self, *, netuid: int, lite: bool):
        self.calls.append((netuid, lite))
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary runtime API failure")
        return {"netuid": netuid}


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str) -> None:
        self.messages.append(message)

    def info(self, message: str) -> None:
        self.messages.append(message)


def test_sync_metagraph_retries_transient_runtime_failure() -> None:
    subtensor = _Subtensor(failures=2)
    logger = _Logger()
    sleeps: list[float] = []

    result = _sync_metagraph(
        subtensor,
        netuid=530,
        logger=logger,
        attempts=3,
        sleep_fn=sleeps.append,
    )

    assert result == {"netuid": 530}
    assert subtensor.calls == [(530, True), (530, True), (530, True)]
    assert sleeps == [3.0, 6.0]
    assert len(logger.messages) == 2


def test_reviewer_candidates_exclude_non_serving_axons() -> None:
    validator = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
    validator.metagraph = SimpleNamespace(
        neurons=[
            SimpleNamespace(
                uid=1,
                hotkey="hotkey_1",
                coldkey="coldkey_1",
                registration_block=10,
                axon_info=SimpleNamespace(ip="127.0.0.1", port=8092, is_serving=True),
            ),
            SimpleNamespace(
                uid=2,
                hotkey="hotkey_2",
                coldkey="coldkey_2",
                registration_block=11,
                axon_info=SimpleNamespace(ip="0.0.0.0", port=0, is_serving=False),
            ),
        ]
    )

    candidates = validator._reviewer_candidates()

    assert [candidate["uid"] for candidate in candidates] == [1]
    assert candidates[0]["axon_port"] == 8092
    assert candidates[0]["is_serving"] is True


def test_reviewer_candidates_respect_target_uids() -> None:
    validator = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
    validator.config = SimpleNamespace(claims_target_uids=[1])
    validator.metagraph = SimpleNamespace(
        neurons=[
            SimpleNamespace(
                uid=1,
                hotkey="hotkey_1",
                coldkey="coldkey_1",
                registration_block=10,
                axon_info=SimpleNamespace(ip="127.0.0.1", port=8092, is_serving=True),
            ),
            SimpleNamespace(
                uid=2,
                hotkey="hotkey_2",
                coldkey="coldkey_2",
                registration_block=11,
                axon_info=SimpleNamespace(ip="127.0.0.2", port=8093, is_serving=True),
            ),
        ]
    )

    assert [candidate["uid"] for candidate in validator._reviewer_candidates()] == [1]


@pytest.mark.parametrize("target_uids", [[], [0, 1, 2]])
def test_reviewer_candidates_exclude_validators_even_when_targeted(target_uids) -> None:
    validator = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
    validator.config = SimpleNamespace(claims_target_uids=target_uids)
    validator.wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="own_hotkey"))
    validator.metagraph = SimpleNamespace(neurons=[
        SimpleNamespace(
            uid=uid,
            hotkey=hotkey,
            coldkey=f"coldkey_{uid}",
            validator_permit=permit,
            axon_info=SimpleNamespace(ip="127.0.0.1", port=8092, is_serving=True),
        )
        for uid, hotkey, permit in [
            (0, "other_validator", True),
            (1, "own_hotkey", False),
            (2, "miner_hotkey", False),
        ]
    ])

    assert [candidate["uid"] for candidate in validator._reviewer_candidates()] == [2]


def test_reviewer_candidates_use_refreshed_permit_status() -> None:
    validator = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
    neuron = SimpleNamespace(
        uid=2,
        hotkey="miner_hotkey",
        coldkey="miner_coldkey",
        validator_permit=False,
        axon_info=SimpleNamespace(ip="127.0.0.1", port=8092, is_serving=True),
    )
    validator.metagraph = SimpleNamespace(neurons=[neuron])
    assert len(validator._reviewer_candidates()) == 1
    neuron.validator_permit = True
    assert validator._reviewer_candidates() == []


def test_seconds_until_deadline_handles_expired_and_future_values() -> None:
    now = datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)

    assert _seconds_until_deadline("2026-09-23T15:59:00Z", now=now) == 0
    assert _seconds_until_deadline("2026-09-23T16:02:00+00:00", now=now) == 120
    assert _seconds_until_deadline("not-a-time", now=now) == 0


def test_consensus_source_failure_must_match_and_fail_validator_verification(monkeypatch) -> None:
    failure = _source_failure_payload(
        '{"schema":"claims_consensus_source_failure_v1","code":"source_download_or_parse_failed",'
        '"paper_id":"paper_1","source_sha256":"abc123"}'
    )
    assert failure is not None
    assignment = {
        "cases": [
            {
                "case": {
                    "source_document": {
                        "paper_id": "paper_1",
                        "source_url": "https://papers.example/paper.pdf",
                        "source_sha256": "abc123",
                    }
                }
            }
        ]
    }
    monkeypatch.setattr(
        "neurons.consensus_validator.download_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("truncated PDF")),
    )
    assert _confirm_source_failure(assignment, failure, timeout=10)

    monkeypatch.setattr("neurons.consensus_validator.download_pdf", lambda *_args, **_kwargs: object())
    assert not _confirm_source_failure(assignment, failure, timeout=10)
    assert not _confirm_source_failure(
        assignment,
        {**failure, "paper_id": "paper_2"},
        timeout=10,
    )


def test_consensus_source_failure_parser_rejects_unstructured_miner_errors() -> None:
    assert _source_failure_payload("PDF download failed") is None
    assert _source_failure_payload('{"schema":"other","paper_id":"p","source_sha256":"h"}') is None


def test_consensus_source_preflight_deduplicates_and_isolates_bad_source(monkeypatch) -> None:
    downloads: list[str] = []

    def download(source_url, **_kwargs):
        downloads.append(source_url)
        if source_url.endswith("bad.pdf"):
            raise ValueError("truncated PDF")
        return object()

    def assignment(hotkey: str, paper_id: str, source_url: str, source_sha256: str) -> dict:
        return {
            "hotkey": hotkey,
            "payload": {
                "cases": [
                    {
                        "case": {
                            "source_document": {
                                "paper_id": paper_id,
                                "source_url": source_url,
                                "source_sha256": source_sha256,
                            }
                        }
                    }
                ]
            },
        }

    monkeypatch.setattr("neurons.consensus_validator.download_pdf", download)
    failed, failures = _preflight_consensus_sources(
        [
            assignment("hotkey_bad_1", "paper_bad", "https://papers.example/bad.pdf", "bad"),
            assignment("hotkey_bad_2", "paper_bad", "https://papers.example/bad.pdf", "bad"),
            assignment("hotkey_good", "paper_good", "https://papers.example/good.pdf", "good"),
        ],
        timeout=10,
        max_workers=4,
    )

    assert failed == ["hotkey_bad_1", "hotkey_bad_2"]
    assert downloads.count("https://papers.example/bad.pdf") == 1
    assert downloads.count("https://papers.example/good.pdf") == 1
    assert failures == [
        {
            "paper_id": "paper_bad",
            "affected_reviewers": 2,
            "reason": "ValueError",
        }
    ]


def test_consensus_source_preflight_rejects_missing_source_document() -> None:
    failed, failures = _preflight_consensus_sources(
        [{"hotkey": "hotkey_1", "payload": {"cases": [{"case": {}}]}}],
        timeout=10,
        max_workers=1,
    )

    assert failed == ["hotkey_1"]
    assert failures == [
        {
            "paper_id": "unknown",
            "affected_reviewers": 1,
            "reason": "missing_source_document",
        }
    ]


def test_expired_round_completes_without_querying_reviewers() -> None:
    completed: list[dict] = []

    class _Backend:
        def complete_miner_consensus_round(self, **kwargs):
            completed.append(kwargs)
            return {"result": {"outcomes": []}}

    validator = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
    validator.metagraph = SimpleNamespace(neurons=[])
    validator.backend_client = _Backend()
    validator.worker_id = "worker_test"
    validator.bt_logging = _Logger()
    validator.config = SimpleNamespace(
        claims_consensus_query_timeout=1800,
        claims_consensus_query_workers=10,
    )

    validator._process_round(
        {
            "round_id": "round_expired",
            "deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "assignments": [
                {
                    "uid": 1,
                    "hotkey": "hotkey_1",
                    "payload": {"cases": [{"item_id": "item_1"}]},
                }
            ],
        }
    )

    assert completed == [
        {
            "round_id": "round_expired",
            "worker_id": "worker_test",
            "submissions": [],
            "validator_failed_hotkeys": [],
        }
    ]
    assert any("Completed expired consensus round" in message for message in validator.bt_logging.messages)


def test_confirmed_source_failure_voids_only_affected_reviewer(monkeypatch) -> None:
    completed: list[dict] = []

    class _Backend:
        def complete_miner_consensus_round(self, **kwargs):
            completed.append(kwargs)
            return {"result": {"outcomes": []}}

    validator = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
    validator.metagraph = SimpleNamespace(
        neurons=[
            SimpleNamespace(hotkey="hotkey_bad", axon_info=SimpleNamespace()),
            SimpleNamespace(hotkey="hotkey_good", axon_info=SimpleNamespace()),
        ]
    )
    validator.backend_client = _Backend()
    validator.worker_id = "worker_test"
    validator.bt_logging = _Logger()
    validator.config = SimpleNamespace(
        claims_consensus_query_timeout=30,
        claims_consensus_query_workers=2,
    )

    def query(_round, assignment, _neuron, _timeout):
        if assignment["hotkey"] == "hotkey_bad":
            return {
                "uid": 1,
                "hotkey": "hotkey_bad",
                "_source_failure": {
                    "paper_id": "paper_bad",
                    "source_sha256": "hash_bad",
                },
            }
        return {
            "uid": 2,
            "hotkey": "hotkey_good",
            "submission_id": "submission_good",
            "response_hash": "hash_good",
        }

    monkeypatch.setattr(validator, "_query_assignment", query)
    monkeypatch.setattr(
        "neurons.consensus_validator._confirm_source_failure",
        lambda *_args, **_kwargs: True,
    )
    validator._process_round(
        {
            "round_id": "round_1",
            "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "assignments": [
                {"uid": 1, "hotkey": "hotkey_bad", "payload": {"cases": []}},
                {"uid": 2, "hotkey": "hotkey_good", "payload": {"cases": []}},
            ],
        }
    )

    assert completed[0]["validator_failed_hotkeys"] == ["hotkey_bad"]
    assert completed[0]["submissions"] == [
        {
            "uid": 2,
            "hotkey": "hotkey_good",
            "submission_id": "submission_good",
            "response_hash": "hash_good",
        }
    ]
