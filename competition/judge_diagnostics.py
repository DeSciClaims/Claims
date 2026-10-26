"""Public per-judge paired-gain diagnostics for the frozen final sample."""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any, Mapping


def final_judge_diagnostics(
    *, sample: list[Mapping[str, Any]], pair_rows: list[Mapping[str, Any]],
    stage: str, finalist_hotkeys: set[str],
) -> dict[str, dict[str, Any]]:
    """Compute J1/J2 gain and the proposal's three-SE configuration flag.

    Infrastructure-fault papers are excluded from both the overall paired
    statistic and these per-judge estimates. Every other row contributes its
    final post-escalation challenger-minus-baseline score.
    """
    if stage not in {"final", "week_one", "rerun"} or not finalist_hotkeys:
        raise ValueError("judge diagnostics need a final stage and finalists")
    assignments = {str(row["paper_id"]): str(row["judge_id"])
                   for row in sample if row["stage"] == stage}
    if (not assignments
        or len(assignments) != sum(row["stage"] == stage for row in sample)
        or set(assignments.values()) != {"J1", "J2"}):
        raise ValueError("final sample has no complete J1/J2 assignments")
    grid: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in pair_rows:
        paper_id = str(row["paper_id"])
        hotkey = str(row["challenger_hotkey"])
        key = (paper_id, hotkey)
        if (paper_id not in assignments or hotkey not in finalist_hotkeys
            or row["judge_id"] != assignments[paper_id] or key in grid):
            raise ValueError("final pair differs from released judge assignments")
        grid[key] = row
    if set(grid) != {(paper_id, hotkey) for paper_id in assignments for hotkey in finalist_hotkeys}:
        raise ValueError("final pair grid is incomplete for judge diagnostics")
    faulty_papers = {paper_id for (paper_id, _), row in grid.items()
                     if row["infrastructure_fault"]}

    result: dict[str, dict[str, Any]] = {}
    for hotkey in sorted(finalist_hotkeys):
        gains_by_judge: dict[str, list[float]] = {"J1": [], "J2": []}
        for paper_id in sorted(assignments):
            row = grid[(paper_id, hotkey)]
            if paper_id in faulty_papers:
                continue
            gain = float(row["challenger_score"]) - float(row["baseline_score"])
            if not math.isfinite(gain) or not -1 <= gain <= 1:
                raise ValueError("final pair has an invalid gain")
            gains_by_judge[assignments[paper_id]].append(gain)
        estimates = {}
        for judge_id, gains in gains_by_judge.items():
            estimates[judge_id] = {
                "paper_count": len(gains),
                "mean_gain": mean(gains) if gains else None,
                "standard_error": stdev(gains) / math.sqrt(len(gains)) if len(gains) >= 2 else None,
            }
        j1, j2 = estimates["J1"], estimates["J2"]
        if j1["standard_error"] is None or j2["standard_error"] is None:
            gap = None
            gap_se = None
            flag = None
        else:
            gap = j1["mean_gain"] - j2["mean_gain"]
            gap_se = math.hypot(j1["standard_error"], j2["standard_error"])
            flag = abs(gap) > 3 * gap_se
        result[hotkey] = {
            "J1": j1, "J2": j2,
            "gain_gap_j1_minus_j2": gap,
            "gap_standard_error": gap_se,
            "review_flag": flag,
        }
    return result
