from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from neurons.consensus_validator import (
    ClaimsConsensusValidator,
    _seconds_until_deadline,
    _sync_metagraph,
)


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


def test_seconds_until_deadline_handles_expired_and_future_values() -> None:
    now = datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)

    assert _seconds_until_deadline("2026-09-23T15:59:00Z", now=now) == 0
    assert _seconds_until_deadline("2026-09-23T16:02:00+00:00", now=now) == 120
    assert _seconds_until_deadline("not-a-time", now=now) == 0


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
