from __future__ import annotations

import random
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any


ALGORITHM_VERSION = "uid_v0_hotkey_history_diverse_v6"
BUCKET_ALGORITHM_VERSION = "bucket_fifo_v1"
VETTED_EVALUATION_COUNT = 3
PERFORMANCE_EVALUATION_COUNT = 1
_LANE_WEIGHTS = (2, 2, 1)


@dataclass(frozen=True)
class MinerSelection:
    neuron: Any
    uid: int
    hotkey: str
    coldkey: str | None
    axon_ip: str | None
    lane: str
    registration_block: int
    immunity_expiry_block: int
    evaluation_count: int
    coldkey_evaluation_count: int
    coldkey_qualification_count: int
    distinct_batch_count: int
    recent_scores: tuple[float, ...]
    performance_score: float
    status: str
    last_selected_batch: str | None
    last_selected_block: int | None
    last_evaluated_block: int | None
    ipv4_proximity_addresses: int
    ipv6_prefix_bits: int

    def assignment(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "hotkey": self.hotkey,
            "coldkey": self.coldkey,
            "axon_ip": self.axon_ip,
            "selection_lane": self.lane,
            "registration_block": self.registration_block,
            "immunity_expiry_block": self.immunity_expiry_block,
            "evaluation_count": self.evaluation_count,
            "coldkey_evaluation_count": self.coldkey_evaluation_count,
            "coldkey_qualification_count": self.coldkey_qualification_count,
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
    zero_score_cooldown_blocks: int = 0,
    ipv4_proximity_addresses: int = 1_024,
    ipv6_prefix_bits: int = 64,
    registration_blocks: dict[int, int] | None = None,
    recent_registration_block: int = 0,
    selection_diagnostics: list[dict[str, Any]] | None = None,
    bucket_max_newcomers_per_batch: int = 5,
) -> list[MinerSelection]:
    miners = [
        _candidate(
            neuron,
            history_rows or [],
            immunity_period_blocks=max(0, int(immunity_period_blocks)),
            ipv4_proximity_addresses=max(0, int(ipv4_proximity_addresses)),
            ipv6_prefix_bits=max(0, min(128, int(ipv6_prefix_bits))),
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
    if mode == "bucket":
        return _select_bucket_miners(
            miners,
            max_newcomers_per_batch=bucket_max_newcomers_per_batch,
            seed=seed,
            current_block=current_block,
            zero_score_cooldown_blocks=zero_score_cooldown_blocks,
            recent_registration_block=recent_registration_block,
            selection_diagnostics=selection_diagnostics,
        )

    qualification_slots, performance_slots, rotation_slots = selection_lane_slots(requested)
    performance_target = qualification_slots + performance_slots

    rng = random.Random(seed)
    selected: list[MinerSelection] = []
    available = [
        item
        for item in miners
        if not _zero_score_cooldown_active(
            item,
            current_block=current_block,
            cooldown_blocks=zero_score_cooldown_blocks,
        )
    ]

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
    _take_until(
        selected,
        available,
        qualification,
        target_total=qualification_slots,
        lane="qualification",
    )
    _fill_oldest(
        selected,
        available,
        qualification_slots,
        lane="qualification",
        recent_registration_block=recent_registration_block,
    )

    performance_candidates = [
        item
        for item in available
        if item.evaluation_count >= PERFORMANCE_EVALUATION_COUNT
        and _latest_score_positive(item)
    ]
    performance = _weighted_sample_without_replacement(
        performance_candidates,
        performance_slots,
        rng,
        already_selected=selected,
    )
    _take(selected, available, performance, lane="performance")

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
    _take_until(
        selected,
        available,
        rotation,
        target_total=min(requested, len(selected) + rotation_slots),
        lane="rotation",
    )
    _fill_oldest(
        selected,
        available,
        requested,
        lane="rotation",
        recent_registration_block=recent_registration_block,
    )

    if selection_diagnostics is not None:
        _record_selection_diagnostics(
            selection_diagnostics,
            miners=miners,
            selected=selected,
            current_block=current_block,
            zero_score_cooldown_blocks=zero_score_cooldown_blocks,
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
    ipv4_proximity_addresses: int,
    ipv6_prefix_bits: int,
    registration_block: int | None,
) -> MinerSelection:
    uid = int(getattr(neuron, "uid", -1))
    hotkey = str(getattr(neuron, "hotkey", "") or "")
    coldkey = str(getattr(neuron, "coldkey", "") or "") or None
    axon = getattr(neuron, "axon_info", None)
    axon_ip = _normalized_ip(getattr(axon, "ip", None))
    registration_block = (
        registration_block_for_neuron(neuron)
        if registration_block is None
        else max(0, int(registration_block))
    )
    history = next(
        (
            row
            for row in history_rows
            if _history_hotkey(row) and _history_hotkey(row) == hotkey
        ),
        {},
    )
    if not history:
        history = next(
            (
                row
                for row in history_rows
                if not _history_hotkey(row)
                and _uid(row.get("uid")) == uid
                and _uid(row.get("registration_block")) == registration_block
            ),
            {},
        )
    count = max(0, _uid(history.get("evaluation_count")))
    distinct_batch_count = max(0, _uid(history.get("distinct_batch_count")))
    scores = tuple(_score(value) for value in list(history.get("recent_scores") or [])[-3:])
    performance_score = sum(scores) / len(scores) if scores else 0.0
    return MinerSelection(
        neuron=neuron,
        uid=uid,
        hotkey=hotkey,
        coldkey=coldkey,
        axon_ip=axon_ip,
        lane="candidate",
        registration_block=registration_block,
        immunity_expiry_block=registration_block + immunity_period_blocks,
        evaluation_count=count,
        coldkey_evaluation_count=max(0, _uid(history.get("coldkey_evaluation_count"))),
        coldkey_qualification_count=max(0, _uid(history.get("coldkey_qualification_count"))),
        distinct_batch_count=distinct_batch_count,
        recent_scores=scores,
        performance_score=performance_score,
        status=_status(count),
        last_selected_batch=str(history.get("last_selected_batch") or "") or None,
        last_selected_block=_optional_int(history.get("last_selected_block")),
        last_evaluated_block=_optional_int(history.get("last_evaluated_block")),
        ipv4_proximity_addresses=ipv4_proximity_addresses,
        ipv6_prefix_bits=ipv6_prefix_bits,
    )


def _select_bucket_miners(
    miners: list[MinerSelection],
    *,
    max_newcomers_per_batch: int,
    seed: str,
    current_block: int,
    zero_score_cooldown_blocks: int,
    recent_registration_block: int,
    selection_diagnostics: list[dict[str, Any]] | None,
) -> list[MinerSelection]:
    selected: list[MinerSelection] = []
    available = [
        item
        for item in miners
        if not _zero_score_cooldown_active(
            item,
            current_block=current_block,
            cooldown_blocks=zero_score_cooldown_blocks,
        )
    ]
    evaluated_coldkeys = {
        _normalized_identity(item.coldkey)
        for item in available
        if (
            item.evaluation_count >= 1
            or item.coldkey_evaluation_count >= 1
            or item.coldkey_qualification_count >= 1
        )
        and _normalized_identity(item.coldkey)
    }

    representatives: dict[str, MinerSelection] = {}
    for item in sorted(available, key=lambda candidate: (candidate.registration_block, candidate.uid, candidate.hotkey)):
        coldkey = _normalized_identity(item.coldkey)
        if (
            not coldkey
            or coldkey in evaluated_coldkeys
            or item.evaluation_count > 0
            or item.coldkey_evaluation_count > 0
            or item.coldkey_qualification_count > 0
            or coldkey in representatives
        ):
            continue
        representatives[coldkey] = item

    newcomers = sorted(
        representatives.values(),
        key=lambda item: (item.registration_block, item.uid, item.hotkey),
    )
    for newcomer in newcomers:
        if len(selected) >= max(1, int(max_newcomers_per_batch)):
            break
        if newcomer not in available or _diversity_conflict(newcomer, selected):
            continue
        selected.append(_with_lane(newcomer, "qualification"))
        available.remove(newcomer)
    selected_newcomer_count = sum(item.lane == "qualification" for item in selected)

    rng = random.Random(seed)
    established = [item for item in available if item.evaluation_count >= 1]
    performance = _weighted_sample_without_replacement(
        [item for item in established if _latest_score_positive(item)],
        4,
        rng,
        already_selected=selected,
    )
    _take(selected, available, performance, lane="performance")

    rotation_target = len(selected) + 4
    rotation = sorted(
        (item for item in available if item.evaluation_count >= 1),
        key=lambda item: (_oldest_first(item.last_evaluated_block), item.uid),
    )
    _take_until(selected, available, rotation, target_total=rotation_target, lane="rotation")

    desired_total = max(10, 8 + selected_newcomer_count)
    established_fill = sorted(
        (item for item in available if item.evaluation_count >= 1),
        key=lambda item: _fallback_order(item, recent_registration_block=recent_registration_block),
    )
    _take_until(
        selected,
        available,
        established_fill,
        target_total=desired_total,
        lane="established-fill",
    )

    if selection_diagnostics is not None:
        _record_selection_diagnostics(
            selection_diagnostics,
            miners=miners,
            selected=selected,
            current_block=current_block,
            zero_score_cooldown_blocks=zero_score_cooldown_blocks,
        )
    return selected


def _weighted_sample_without_replacement(
    candidates: list[MinerSelection],
    count: int,
    rng: random.Random,
    *,
    already_selected: list[MinerSelection] | None = None,
) -> list[MinerSelection]:
    selected = list(already_selected or [])
    pool = sorted(
        (item for item in candidates if not _diversity_conflict(item, selected)),
        key=lambda item: item.uid,
    )
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
        picked = pool.pop(index)
        chosen.append(picked)
        selected.append(picked)
        pool = [item for item in pool if not _diversity_conflict(item, selected)]
    return chosen


def _take(
    selected: list[MinerSelection],
    available: list[MinerSelection],
    candidates: list[MinerSelection],
    *,
    lane: str,
) -> None:
    for item in candidates:
        if item not in available or _diversity_conflict(item, selected):
            continue
        selected.append(_with_lane(item, lane))
        available.remove(item)


def _take_until(
    selected: list[MinerSelection],
    available: list[MinerSelection],
    candidates: list[MinerSelection],
    *,
    target_total: int,
    lane: str,
) -> None:
    for item in candidates:
        if len(selected) >= target_total:
            break
        _take(selected, available, [item], lane=lane)


def _fill_oldest(
    selected: list[MinerSelection],
    available: list[MinerSelection],
    target_total: int,
    *,
    lane: str,
    recent_registration_block: int,
) -> None:
    fillers = sorted(
        available,
        key=lambda item: _fallback_order(item, recent_registration_block=recent_registration_block),
    )
    _take_until(
        selected,
        available,
        fillers,
        target_total=target_total,
        lane=lane,
    )


def _fallback_order(item: MinerSelection, *, recent_registration_block: int) -> tuple[int, int, int, int]:
    cutoff = max(0, int(recent_registration_block))
    return (
        0 if item.evaluation_count == 0 else 1,
        _oldest_first(item.last_evaluated_block),
        0 if cutoff > 0 and item.registration_block >= cutoff else 1 if cutoff > 0 else 0,
        item.uid,
    )


def _immunity_priority(item: MinerSelection, current_block: int, priority_blocks: int) -> bool:
    return (
        item.evaluation_count < VETTED_EVALUATION_COUNT
        and (item.evaluation_count == 0 or _latest_score_positive(item))
        and item.immunity_expiry_block - max(0, int(current_block)) <= max(0, int(priority_blocks))
    )


def _latest_score_positive(item: MinerSelection) -> bool:
    return bool(item.recent_scores) and item.recent_scores[-1] > 0.0


def _zero_score_cooldown_active(
    item: MinerSelection,
    *,
    current_block: int,
    cooldown_blocks: int,
) -> bool:
    if not item.recent_scores or item.recent_scores[-1] > 0.0:
        return False
    cooldown = max(0, int(cooldown_blocks))
    anchor = item.last_evaluated_block
    if anchor is None:
        anchor = item.last_selected_block
    return cooldown > 0 and anchor is not None and max(0, int(current_block)) < int(anchor) + cooldown


def _with_lane(item: MinerSelection, lane: str) -> MinerSelection:
    return MinerSelection(**{**item.__dict__, "lane": lane})


def _diversity_conflict(item: MinerSelection, selected: list[MinerSelection]) -> bool:
    return _diversity_conflict_reason(item, selected) is not None


def _diversity_conflict_reason(
    item: MinerSelection,
    selected: list[MinerSelection],
) -> dict[str, Any] | None:
    coldkey = _normalized_identity(item.coldkey)
    axon_ip = _normalized_ip(item.axon_ip)
    parsed_ip = _parsed_ip(axon_ip)
    for existing in selected:
        if coldkey and coldkey == _normalized_identity(existing.coldkey):
            return {
                "reason": "coldkey_conflict",
                "conflicting_uid": existing.uid,
                "conflicting_hotkey": existing.hotkey,
                "coldkey": coldkey,
                "conflicting_coldkey": _normalized_identity(existing.coldkey),
            }
        existing_ip = _normalized_ip(existing.axon_ip)
        if axon_ip and axon_ip == existing_ip:
            return {
                "reason": "axon_ip_exact_conflict",
                "conflicting_uid": existing.uid,
                "conflicting_hotkey": existing.hotkey,
                "axon_ip": axon_ip,
                "conflicting_coldkey": _normalized_identity(existing.coldkey),
            }
        parsed_existing = _parsed_ip(existing_ip)
        if isinstance(parsed_ip, IPv4Address) and isinstance(parsed_existing, IPv4Address):
            distance = abs(int(parsed_ip) - int(parsed_existing))
            if item.ipv4_proximity_addresses > 0 and distance <= item.ipv4_proximity_addresses:
                return {
                    "reason": "axon_ipv4_proximity_conflict",
                    "conflicting_uid": existing.uid,
                    "conflicting_hotkey": existing.hotkey,
                    "axon_ip": axon_ip,
                    "conflicting_axon_ip": existing_ip,
                    "conflicting_coldkey": _normalized_identity(existing.coldkey),
                    "address_distance": distance,
                }
        if isinstance(parsed_ip, IPv6Address) and isinstance(parsed_existing, IPv6Address):
            prefix_bits = max(0, min(128, item.ipv6_prefix_bits))
            network = ip_network((parsed_ip, prefix_bits), strict=False)
            existing_network = ip_network((parsed_existing, prefix_bits), strict=False)
            if network == existing_network:
                return {
                    "reason": "axon_ipv6_prefix_conflict",
                    "conflicting_uid": existing.uid,
                    "conflicting_hotkey": existing.hotkey,
                    "axon_ip": axon_ip,
                    "conflicting_axon_ip": existing_ip,
                    "conflicting_coldkey": _normalized_identity(existing.coldkey),
                    "network": str(network),
                }
    return None


def _record_selection_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    miners: list[MinerSelection],
    selected: list[MinerSelection],
    current_block: int,
    zero_score_cooldown_blocks: int,
) -> None:
    selected_uids = {item.uid for item in selected}
    for item in miners:
        if item.uid in selected_uids:
            continue
        if _zero_score_cooldown_active(
            item,
            current_block=current_block,
            cooldown_blocks=zero_score_cooldown_blocks,
        ):
            anchor = item.last_evaluated_block
            if anchor is None:
                anchor = item.last_selected_block
            diagnostics.append(
                {
                    "uid": item.uid,
                    "hotkey": item.hotkey,
                    "coldkey": item.coldkey,
                    "axon_ip": item.axon_ip,
                    "reason": "zero_score_cooldown",
                    "eligible_after_block": int(anchor or 0) + max(0, int(zero_score_cooldown_blocks)),
                }
            )
            continue
        conflict = _diversity_conflict_reason(item, selected)
        if conflict is not None:
            diagnostics.append(
                {
                    "uid": item.uid,
                    "hotkey": item.hotkey,
                    "coldkey": item.coldkey,
                    "axon_ip": item.axon_ip,
                    **conflict,
                }
            )


def _history_hotkey(row: dict[str, Any]) -> str:
    return str(row.get("miner_hotkey") or row.get("hotkey") or "").strip()


def _normalized_identity(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalized_ip(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().strip("[]")
    parsed = _parsed_ip(normalized)
    return str(parsed) if parsed is not None else normalized or None


def _parsed_ip(value: Any) -> IPv4Address | IPv6Address | None:
    normalized = str(value or "").strip().lower().strip("[]")
    if not normalized:
        return None
    try:
        parsed = ip_address(normalized)
    except ValueError:
        return None
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


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
