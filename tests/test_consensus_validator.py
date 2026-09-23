from __future__ import annotations

from types import SimpleNamespace

from neurons.consensus_validator import ClaimsConsensusValidator, _sync_metagraph


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
