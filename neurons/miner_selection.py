from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


ALGORITHM_VERSION = "uid_v0_proportional_provisional_v1"
VETTED_EVALUATION_COUNT = 3
_LANE_WEIGHTS = (2, 2, 1)


@dataclass(frozen=True)
class MinerSelection:
    neuron: Any
    uid: int
    hotkey: str
    coldkey: str | None
    lane: str
    registration_block: int
    immunity_expiry_block: int
    evaluation_count: int
    distinct_batch_count: int
    recent_scores: tuple[float, ...]
    performance_score: float
    status: str
    last_selected_batch: str | None
    last_selected_block: int | None
    last_evaluated_block: int | None

    def assignment(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "hotkey": self.hotkey,
            "coldkey": self.coldkey,
            "selection_lane": self.lane,
            "registration_block": self.registration_block,
            "immunity_expiry_block": self.immunity_expiry_block,
            "evaluation_count": self.evaluation_count,
            "distinct_batch_count": self.distinct_batch_count,
            "recent_scores": list(self.recent_scores),
            "performance_score": round(self.performance_score, 6),
            "selection_status": self.status,
            "last_selected_batch": self.last_selected_batch,
            "last_selected_block": self.last_selected_block,
            "last_evaluated_block": self.last_evaluated_block,
            # Keep the original snapshot keys readable by older backend deployments.
            "historical_batch_count": self.evaluation_count,
            "historical_average_score": round(self.performance_score, 6) if self.recent_scores else None,
            "smoothed_score": round(self.performance_score, 6),
            "last_scored_at": None,
        }


def select_miners(
    candidates: list[Any],
    *,
    history_rows: list[dict[str, Any]] | None,
    sample_size: int,
    seed: str,
    mode: str = "adaptive",
    current_block: int = 0,
    immunity_period_blocks: int = 0,
    immunity_priority_blocks: int = 7_200,
    registration_blocks: dict[int, int] | None = None,
    recent_registration_block: int = 0,
) -> list[MinerSelection]:
    miners = [
        _candidate(
            neuron,
            history_rows or [],
            immunity_period_blocks=max(0, int(immunity_period_blocks)),
            registration_block=(registration_blocks or {}).get(int(getattr(neuron, "uid", -1))),
        )
        for neuron in candidates
    ]
    miners.sort(key=lambda item: item.uid)
    if not miners:
        return []

    requested = max(1, int(sample_size))
    if mode == "all":
        return [_with_lane(item, "all") for item in miners]

    qualification_slots, performance_slots, rotation_slots = selection_lane_slots(requested)
    performance_target = qualification_slots + performance_slots

    rng = random.Random(seed)
    selected: list[MinerSelection] = []
    available = list(miners)

    qualification = [item for item in available if item.evaluation_count < VETTED_EVALUATION_COUNT]
    qualification.sort(
        key=lambda item: (
            0 if _immunity_priority(item, current_block, immunity_priority_blocks) else 1,
            item.evaluation_count,
            item.immunity_expiry_block,
            _oldest_first(item.last_selected_block),
            item.uid,
        )
    )
    _take(selected, available, qualification[:qualification_slots], lane="qualification")
    _fill_oldest(
        selected,
        available,
        qualification_slots,
        lane="qualification",
        recent_registration_block=recent_registration_block,
    )

    vetted = [item for item in available if item.evaluation_count >= VETTED_EVALUATION_COUNT]
    performance = _weighted_sample_without_replacement(vetted, performance_slots, rng)
    _take(selected, available, performance, lane="performance")

    provisional = [
        item
        for item in available
        if 0 < item.evaluation_count < VETTED_EVALUATION_COUNT
        and any(score > 0.0 for score in item.recent_scores)
    ]
    provisional_performance = _weighted_sample_without_replacement(
        provisional,
        max(0, performance_target - len(selected)),
        rng,
    )
    _take(selected, available, provisional_performance, lane="performance-provisional")
    _fill_oldest(
        selected,
        available,
        performance_target,
        lane="performance-fallback",
        recent_registration_block=recent_registration_block,
    )

    rotation = sorted(
        available,
        key=lambda item: _fallback_order(item, recent_registration_block=recent_registration_block),
    )
    _take(selected, available, rotation[:rotation_slots], lane="rotation")
    _fill_oldest(
        selected,
        available,
        requested,
        lane="rotation",
        recent_registration_block=recent_registration_block,
    )

    return selected


def selection_lane_slots(sample_size: int) -> tuple[int, int, int]:
    """Apportion the configured total across 40/40/20 V0 lanes."""
    requested = max(1, int(sample_size))
    weight_total = sum(_LANE_WEIGHTS)
    slots = [requested * weight // weight_total for weight in _LANE_WEIGHTS]
    remainders = [requested * weight % weight_total for weight in _LANE_WEIGHTS]
    for index in sorted(range(len(slots)), key=lambda item: (-remainders[item], item))[
        : requested - sum(slots)
    ]:
        slots[index] += 1
    return slots[0], slots[1], slots[2]


def _candidate(
    neuron: Any,
    history_rows: list[dict[str, Any]],
    *,
    immunity_period_blocks: int,
    registration_block: int | None,
) -> MinerSelection:
    uid = int(getattr(neuron, "uid", -1))
    hotkey = str(getattr(neuron, "hotkey", "") or "")
    coldkey = str(getattr(neuron, "coldkey", "") or "") or None
    registration_block = (
        registration_block_for_neuron(neuron)
        if registration_block is None
        else max(0, int(registration_block))
    )
    history = next((row for row in history_rows if _uid(row.get("uid")) == uid), {})
    if _uid(history.get("registration_block")) != registration_block:
        history = {}
    count = max(0, _uid(history.get("evaluation_count")))
    distinct_batch_count = max(0, _uid(history.get("distinct_batch_count")))
    scores = tuple(_score(value) for value in list(history.get("recent_scores") or [])[-3:])
    performance_score = sum(scores) / len(scores) if scores else 0.0
    return MinerSelection(
        neuron=neuron,
        uid=uid,
        hotkey=hotkey,
        coldkey=coldkey,
        lane="candidate",
        registration_block=registration_block,
        immunity_expiry_block=registration_block + immunity_period_blocks,
        evaluation_count=count,
        distinct_batch_count=distinct_batch_count,
        recent_scores=scores,
        performance_score=performance_score,
        status=_status(count),
        last_selected_batch=str(history.get("last_selected_batch") or "") or None,
        last_selected_block=_optional_int(history.get("last_selected_block")),
        last_evaluated_block=_optional_int(history.get("last_evaluated_block")),
    )


def _weighted_sample_without_replacement(
    candidates: list[MinerSelection],
    count: int,
    rng: random.Random,
) -> list[MinerSelection]:
    pool = sorted(candidates, key=lambda item: item.uid)
    chosen: list[MinerSelection] = []
    while pool and len(chosen) < count:
        weights = [0.10 + item.performance_score for item in pool]
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


def _take(
    selected: list[MinerSelection],
    available: list[MinerSelection],
    candidates: list[MinerSelection],
    *,
    lane: str,
) -> None:
    for item in candidates:
        if item not in available:
            continue
        selected.append(_with_lane(item, lane))
        available.remove(item)


def _fill_oldest(
    selected: list[MinerSelection],
    available: list[MinerSelection],
    target_total: int,
    *,
    lane: str,
    recent_registration_block: int,
) -> None:
    needed = min(max(0, target_total - len(selected)), len(available))
    fillers = sorted(
        available,
        key=lambda item: _fallback_order(item, recent_registration_block=recent_registration_block),
    )[:needed]
    _take(selected, available, fillers, lane=lane)


def _fallback_order(item: MinerSelection, *, recent_registration_block: int) -> tuple[int, int, int]:
    cutoff = max(0, int(recent_registration_block))
    return (
        _oldest_first(item.last_evaluated_block),
        0 if cutoff > 0 and item.registration_block >= cutoff else 1 if cutoff > 0 else 0,
        item.uid,
    )


def _immunity_priority(item: MinerSelection, current_block: int, priority_blocks: int) -> bool:
    return (
        item.evaluation_count < VETTED_EVALUATION_COUNT
        and item.immunity_expiry_block - max(0, int(current_block)) <= max(0, int(priority_blocks))
    )


def _with_lane(item: MinerSelection, lane: str) -> MinerSelection:
    return MinerSelection(**{**item.__dict__, "lane": lane})


def registration_block_for_neuron(neuron: Any) -> int:
    for name in ("block_at_registration", "registration_block", "registered_at"):
        value = getattr(neuron, name, None)
        parsed = _optional_int(value)
        if parsed is not None:
            return max(0, parsed)
    return 0


def _status(count: int) -> str:
    if count <= 0:
        return "new"
    if count < VETTED_EVALUATION_COUNT:
        return "under-vetted"
    return "vetted"


def _oldest_first(value: int | None) -> int:
    return -1 if value is None else int(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        if hasattr(value, "item"):
            value = value.item()
        return int(value)
    except (TypeError, ValueError):
        return None


def _uid(value: Any) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None else -1


def _score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
