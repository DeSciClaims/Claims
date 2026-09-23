from __future__ import annotations

from neurons.consensus_validator import _sync_metagraph


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
