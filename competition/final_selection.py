"""Choose the production package from a complete, paired final score grid."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import stdev

from .policy import PairedResult, PaperPairScore, crown_eligible, paired_result


@dataclass(frozen=True)
class Finalist:
    hotkey: str
    team_id: str
    commit_block: int
    papers: tuple[PaperPairScore, ...]
    code_passed: bool
    copy: bool
    gold_refused: bool


@dataclass(frozen=True)
class FinalistVerdict:
    hotkey: str
    team_id: str
    mean_gain: float
    standard_error: float
    paper_count: int
    cleared: bool
    reason: str


@dataclass(frozen=True)
class FinalSelection:
    production_hotkey: str | None
    decision: str
    winner_gain: float
    verdicts: tuple[FinalistVerdict, ...]


def _difference_se(left: PairedResult, right: PairedResult) -> float:
    if left.paper_count != right.paper_count or len(left.gains) != len(right.gains):
        raise ValueError("finalists must share one eligible paper grid")
    if left.paper_count < 2:
        return math.inf
    differences = [a - b for a, b in zip(left.gains, right.gains, strict=True)]
    return stdev(differences) / math.sqrt(len(differences))


def select_final_production(
    finalists: list[Finalist], *, incumbent_hotkey: str | None,
    incumbent_team_id: str | None = None,
) -> FinalSelection:
    """Apply code, copy, Gold, statistical, own-version, and tie rules.

    An infrastructure fault must be global to the paper before this function.
    The caller supplies post-escalation scores and handles the >20% incumbent
    failure fallback before calculating these paired rows.
    """
    if not finalists:
        return FinalSelection(incumbent_hotkey, "incumbent_continues" if incumbent_hotkey else "no_crown", 0.0, ())
    if bool(incumbent_hotkey) != bool(incumbent_team_id):
        raise ValueError("incumbent hotkey and team must be supplied together")
    if len({row.hotkey for row in finalists}) != len(finalists):
        raise ValueError("finalists must have distinct hotkeys")
    expected: tuple[tuple[str, bool], ...] | None = None
    for row in finalists:
        if not row.hotkey or not row.team_id or row.commit_block < 0:
            raise ValueError("finalist identity or commitment is invalid")
        grid = tuple((paper.paper_id, paper.infrastructure_fault) for paper in row.papers)
        if expected is None:
            expected = grid
        elif grid != expected:
            raise ValueError("finalists must share the same ordered paper and fault grid")
    scored = {row.hotkey: paired_result(row.papers) for row in finalists}
    verdicts: list[FinalistVerdict] = []
    clearers: list[Finalist] = []
    for row in finalists:
        result = scored[row.hotkey]
        own_version = incumbent_team_id is not None and row.team_id == incumbent_team_id
        if not row.code_passed:
            reason = "code_failed"
        elif row.copy and not own_version:
            reason = "copy"
        elif own_version:
            reason = "cleared_own_version" if result.mean_gain >= 0 else "negative_own_version"
        elif row.gold_refused:
            reason = "gold_refused"
        elif crown_eligible(result, gold_refused=False, week_one=incumbent_hotkey is None):
            reason = "cleared"
        else:
            reason = "below_crown_bar"
        cleared = reason in {"cleared", "cleared_own_version"}
        verdicts.append(FinalistVerdict(
            row.hotkey, row.team_id, result.mean_gain, result.standard_error,
            result.paper_count, cleared, reason,
        ))
        if cleared:
            clearers.append(row)
    if not clearers:
        return FinalSelection(incumbent_hotkey,
                              "incumbent_continues" if incumbent_hotkey else "no_crown",
                              0.0, tuple(verdicts))
    ranked = sorted(clearers, key=lambda row: (-scored[row.hotkey].mean_gain,
                                               row.commit_block, row.hotkey))
    leader = ranked[0]
    within_one_se = [row for row in ranked if
                     scored[leader.hotkey].mean_gain - scored[row.hotkey].mean_gain
                     <= _difference_se(scored[leader.hotkey], scored[row.hotkey])]
    winner = min(within_one_se, key=lambda row: (row.commit_block, row.hotkey))
    own = incumbent_team_id is not None and winner.team_id == incumbent_team_id
    return FinalSelection(
        winner.hotkey, "incumbent_version_swap" if own else "new_crown",
        scored[winner.hotkey].mean_gain, tuple(verdicts),
    )
