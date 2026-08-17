from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any


ALGORITHM_VERSION = "adaptive_v1"


@dataclass(frozen=True)
class MinerSelection:
    neuron: Any
    uid: int
    hotkey: str
    coldkey: str | None
    lane: str
    historical_batch_count: int
    historical_average_score: float | None
    smoothed_score: float
    last_scored_at: str | None

    def assignment(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "hotkey": self.hotkey,
            "coldkey": self.coldkey,
            "selection_lane": self.lane,
            "historical_batch_count": self.historical_batch_count,
            "historical_average_score": self.historical_average_score,
            "smoothed_score": round(self.smoothed_score, 6),
            "last_scored_at": self.last_scored_at,
        }


def select_miners(
    candidates: list[Any],
    *,
    history_rows: list[dict[str, Any]] | None,
    sample_size: int,
    seed: str,
    mode: str = "adaptive",
    new_miner_batch_threshold: int = 3,
) -> list[MinerSelection]:
    miners = [_candidate(neuron, history_rows or []) for neuron in candidates]
    miners.sort(key=lambda item: (item.uid, item.hotkey))
    if not miners:
        return []

    requested = max(1, int(sample_size))
    if mode == "all" or requested >= len(miners):
        return [_with_lane(item, "all") for item in miners]

    rng = random.Random(seed)
    selected: list[MinerSelection] = []
    available = list(miners)

    development_slots = max(1, round(requested * 0.20)) if requested >= 3 else 0
    exploration_slots = max(1, round(requested * 0.20)) if requested >= 2 else 0
    if development_slots + exploration_slots >= requested:
        exploration_slots = max(0, requested - development_slots - 1)

    under_tested = [item for item in available if item.historical_batch_count < new_miner_batch_threshold]
    under_tested.sort(
        key=lambda item: (
            item.historical_batch_count,
            item.last_scored_at or "",
            _tie_break(seed, item.hotkey),
        )
    )
    for item in under_tested[:development_slots]:
        selected.append(_with_lane(item, "development"))
        available.remove(item)

    performance_slots = max(0, requested - len(selected) - exploration_slots)
    for item in _weighted_sample_without_replacement(available, performance_slots, rng):
        selected.append(_with_lane(item, "performance"))
        available.remove(item)

    exploration_count = min(exploration_slots, requested - len(selected), len(available))
    for item in rng.sample(available, exploration_count):
        selected.append(_with_lane(item, "exploration"))
        available.remove(item)

    remaining = requested - len(selected)
    if remaining > 0:
        for item in _weighted_sample_without_replacement(available, remaining, rng):
            selected.append(_with_lane(item, "performance"))

    selected.sort(key=lambda item: (item.uid, item.hotkey))
    return selected


def _candidate(neuron: Any, history_rows: list[dict[str, Any]]) -> MinerSelection:
    uid = int(getattr(neuron, "uid", -1))
    hotkey = str(getattr(neuron, "hotkey", "") or "")
    coldkey = str(getattr(neuron, "coldkey", "") or "") or None
    history = next((row for row in history_rows if str(row.get("hotkey") or "") == hotkey), {})
    count = max(
        0,
        int(
            history.get("selected_run_count")
            or history.get("report_count")
            or history.get("completed_batch_count")
            or 0
        ),
    )
    average = _score(history.get("average_score"))
    network_mean = _network_mean(history_rows)
    smoothed = _smoothed_score(average, count, network_mean)
    return MinerSelection(
        neuron=neuron,
        uid=uid,
        hotkey=hotkey,
        coldkey=coldkey,
        lane="candidate",
        historical_batch_count=count,
        historical_average_score=average,
        smoothed_score=smoothed,
        last_scored_at=str(
            history.get("latest_report_at")
            or history.get("last_scored_at")
            or history.get("last_selected_at")
            or ""
        )
        or None,
    )


def _network_mean(rows: list[dict[str, Any]]) -> float:
    weighted_total = 0.0
    count_total = 0
    for row in rows:
        count = max(0, int(row.get("report_count") or row.get("completed_batch_count") or 0))
        average = _score(row.get("average_score"))
        if average is None or count <= 0:
            continue
        weighted_total += average * count
        count_total += count
    return weighted_total / count_total if count_total else 0.5


def _smoothed_score(average: float | None, count: int, network_mean: float, prior_weight: float = 3.0) -> float:
    observed = average if average is not None else network_mean
    return ((network_mean * prior_weight) + (observed * count)) / (prior_weight + count)


def _weighted_sample_without_replacement(
    candidates: list[MinerSelection],
    count: int,
    rng: random.Random,
) -> list[MinerSelection]:
    pool = list(candidates)
    chosen: list[MinerSelection] = []
    while pool and len(chosen) < count:
        # The floor preserves a non-zero chance for historically weak miners.
        weights = [0.05 + math.pow(max(0.0, min(1.0, item.smoothed_score)), 2) for item in pool]
        pick = rng.random() * sum(weights)
        cumulative = 0.0
        index = len(pool) - 1
        for candidate_index, weight in enumerate(weights):
            cumulative += weight
            if pick <= cumulative:
                index = candidate_index
                break
        chosen.append(pool.pop(index))
    return chosen


def _with_lane(item: MinerSelection, lane: str) -> MinerSelection:
    return MinerSelection(
        neuron=item.neuron,
        uid=item.uid,
        hotkey=item.hotkey,
        coldkey=item.coldkey,
        lane=lane,
        historical_batch_count=item.historical_batch_count,
        historical_average_score=item.historical_average_score,
        smoothed_score=item.smoothed_score,
        last_scored_at=item.last_scored_at,
    )


def _score(value: Any) -> float | None:
    if not isinstance(value, int | float):
        return None
    return max(0.0, min(1.0, float(value)))


def _tie_break(seed: str, hotkey: str) -> str:
    return hashlib.sha256(f"{seed}:{hotkey}".encode()).hexdigest()
