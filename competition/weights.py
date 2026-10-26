"""Pure weekly 90/10 weight allocation; chain submission is a separate step."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Payee:
    hotkey: str
    team_id: str
    qualified: bool
    gain: float = 0.0
    copy: bool = False
    code_passed: bool = True


@dataclass(frozen=True)
class WeightPlan:
    weights_by_uid: dict[int, float]
    roles: tuple[tuple[str, str, float], ...]
    burned_fraction: float


def weekly_weight_plan(
    *, operator_hotkey: str, production: Payee | None,
    finalists: list[Payee], uid_by_hotkey: Mapping[str, int],
    burn_uid: int = 0, winner_gain: float = 0.0,
) -> WeightPlan:
    """Allocate 70/20/10 of the miner pool; never reassign vacant slots.

    The caller must verify that the chain actually treats ``burn_uid`` as a
    burn and must re-resolve all hotkeys to UIDs at every weight heartbeat.
    A missing/unqualified production payee burns its 70% miner slot.
    """
    operator_uid = uid_by_hotkey.get(operator_hotkey)
    if operator_uid is None or operator_uid == burn_uid or operator_uid < 0 or burn_uid < 0:
        raise ValueError("operator and burn UIDs must be distinct registered UIDs")
    if not math.isfinite(winner_gain):
        raise ValueError("winner gain must be finite")
    hotkeys = [item.hotkey for item in finalists]
    if len(hotkeys) != len(set(hotkeys)):
        raise ValueError("finalists must have distinct hotkeys")
    for item in finalists + ([production] if production else []):
        if not item.hotkey or not item.team_id or not math.isfinite(item.gain):
            raise ValueError("payee has invalid identity or gain")
    weights: dict[int, float] = {operator_uid: 0.90}
    roles: list[tuple[str, str, float]] = [(operator_hotkey, "operator", 0.90)]
    burned = 0.0

    def assign(role: str, item: Payee | None, share: float) -> None:
        nonlocal burned
        uid = uid_by_hotkey.get(item.hotkey) if item and item.qualified else None
        if uid is None:
            burned += share
            roles.append((item.hotkey if item else "", role + "_burned", share))
            return
        if uid == burn_uid or uid == operator_uid or uid < 0:
            raise ValueError("payee resolves to reserved UID")
        weights[uid] = weights.get(uid, 0.0) + share
        roles.append((item.hotkey, role, share))

    if production and (production.copy or not production.code_passed):
        raise ValueError("production package must pass copy and code checks")
    assign("production", production, 0.07)
    eligible = sorted(
        (
            item for item in finalists
            if item.qualified and item.code_passed and not item.copy
            and item.gain >= winner_gain - 0.05
            and (production is None or item.team_id != production.team_id)
        ),
        key=lambda item: (-item.gain, item.hotkey),
    )
    paid_teams = {production.team_id} if production else set()
    runners: list[Payee] = []
    for item in eligible:
        if item.team_id in paid_teams:
            continue
        runners.append(item)
        paid_teams.add(item.team_id)
        if len(runners) == 2:
            break
    for index, share in enumerate((0.02, 0.01)):
        assign(f"runner_up_{index + 1}", runners[index] if index < len(runners) else None, share)
    if burned:
        weights[burn_uid] = burned
    if abs(sum(weights.values()) - 1.0) > 1e-12:
        raise AssertionError("weekly weight vector does not sum to one")
    return WeightPlan(dict(sorted(weights.items())), tuple(roles), burned)
