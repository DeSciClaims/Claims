from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is optional at import time
    psutil = None  # type: ignore[assignment]


_MIB = 1024 * 1024


@dataclass(frozen=True)
class MemorySample:
    timestamp: float
    process_tree_rss_bytes: int
    system_available_bytes: int
    process_count: int


class ValidatorMemorySampler:
    """Samples validator and local child-process RSS for one validator run."""

    def __init__(self, *, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._samples: list[MemorySample] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return psutil is not None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._sample()
        self._thread = threading.Thread(target=self._run, name="claims-validator-memory", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self._sample()

    def summary(self) -> dict[str, Any]:
        return _summarize_samples(self.samples(), interval_seconds=self.interval_seconds)

    def interval_summary(self, started_at: str, ended_at: str) -> dict[str, Any]:
        start = _iso_timestamp(started_at)
        end = _iso_timestamp(ended_at)
        if start is None or end is None or end < start:
            return {}
        samples = self.samples()
        selected = [sample for sample in samples if start <= sample.timestamp <= end]
        if not selected and samples:
            midpoint = start + ((end - start) / 2)
            nearest = min(samples, key=lambda sample: abs(sample.timestamp - midpoint))
            if abs(nearest.timestamp - midpoint) <= self.interval_seconds * 2:
                selected = [nearest]
        summary = _summarize_samples(selected, interval_seconds=self.interval_seconds)
        summary.pop("schema", None)
        summary.pop("sample_interval_seconds", None)
        return summary

    def samples(self) -> list[MemorySample]:
        with self._lock:
            return list(self._samples)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        sample = _read_process_tree_sample()
        if sample is None:
            return
        with self._lock:
            self._samples.append(sample)


def _read_process_tree_sample() -> MemorySample | None:
    if psutil is None:
        return None
    try:
        root = psutil.Process(os.getpid())
        processes = [root, *root.children(recursive=True)]
        seen: set[int] = set()
        rss_bytes = 0
        process_count = 0
        for process in processes:
            if process.pid in seen:
                continue
            seen.add(process.pid)
            try:
                rss_bytes += int(process.memory_info().rss)
                process_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        available_bytes = int(psutil.virtual_memory().available)
        return MemorySample(
            timestamp=time.time(),
            process_tree_rss_bytes=rss_bytes,
            system_available_bytes=available_bytes,
            process_count=process_count,
        )
    except (psutil.Error, OSError):
        return None


def _summarize_samples(samples: list[MemorySample], *, interval_seconds: float) -> dict[str, Any]:
    if not samples:
        return {}
    return {
        "schema": "claims_validator_memory_v1",
        "sample_interval_seconds": round(float(interval_seconds), 3),
        "sample_count": len(samples),
        "process_tree_rss_start_mb": _mb(samples[0].process_tree_rss_bytes),
        "process_tree_rss_end_mb": _mb(samples[-1].process_tree_rss_bytes),
        "process_tree_rss_peak_mb": _mb(max(sample.process_tree_rss_bytes for sample in samples)),
        "system_available_min_mb": _mb(min(sample.system_available_bytes for sample in samples)),
        "process_count_peak": max(sample.process_count for sample in samples),
    }


def _iso_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _mb(value: int) -> float:
    return round(float(value) / _MIB, 3)
