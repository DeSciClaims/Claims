"""Deterministic entry and advancement rules for the weekly stages."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


Stage = Literal["prescreen", "screen", "final", "week_one"]


@dataclass(frozen=True)
class StageCandidate:
    hotkey: str
    team_id: str
    paired_gain: float
    commit_block: int


def stage_plan(*, challenger_count: int, week_one: bool = False) -> tuple[Stage, ...]:
    if challenger_count < 0:
        raise ValueError("challenger count cannot be negative")
    if not challenger_count:
        return ()
    if week_one:
        return ("week_one",)
    if challenger_count <= 3:
        return ("final",)
    if challenger_count <= 12:
        return ("screen", "final")
    return ("prescreen", "screen", "final")


def advance(
    candidates: list[StageCandidate], *, stage: Literal["prescreen", "screen"],
    limit: int | None = None, per_team: int | None = None,
) -> tuple[StageCandidate, ...]:
    """Rank paired gain and apply the proposal's per-team stage caps.

    Unresolved infrastructure faults must be removed from the stage before
    calling this function; package faults have already scored zero per paper.
    """
    default_limit, default_per_team = ((12, 2) if stage == "prescreen"
                                      else (3, 1) if stage == "screen" else (0, 0))
    limit = default_limit if limit is None else limit
    per_team = default_per_team if per_team is None else per_team
    if not limit:
        raise ValueError("advancement is only defined for prescreen or screen")
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.hotkey or not candidate.team_id or candidate.hotkey in seen:
            raise ValueError("stage candidates need distinct hotkeys and team IDs")
        if not math.isfinite(candidate.paired_gain) or candidate.commit_block < 0:
            raise ValueError("stage candidates need finite scores and valid commit blocks")
        seen.add(candidate.hotkey)
    ranked = sorted(candidates, key=lambda candidate: (-candidate.paired_gain, candidate.commit_block, candidate.hotkey))
    team_count: dict[str, int] = {}
    selected: list[StageCandidate] = []
    for candidate in ranked:
        if team_count.get(candidate.team_id, 0) >= per_team:
            continue
        selected.append(candidate)
        team_count[candidate.team_id] = team_count.get(candidate.team_id, 0) + 1
        if len(selected) == limit:
            break
    return tuple(selected)
