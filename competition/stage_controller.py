"""Resume a staged weekly evaluation from durable backend receipts."""

from __future__ import annotations

from typing import Any, Mapping

from .intake_lock import CompetitionBackend
from .jobs import build_jobs
from .policy import PaperPairScore, paired_result
from .stages import StageCandidate, advance, stage_plan


def _enqueue_current_stage(
    round_id: str, *, backend: CompetitionBackend,
    round_record: Mapping[str, Any], receipt: Mapping[str, Any],
    sample: list[dict[str, Any]],
) -> None:
    jobs = build_jobs(
        round_id=round_id, selected=receipt["entries"], sample=sample,
        stage=receipt["stage"],
        configuration_sha256=round_record["configuration_sha256"],
    )
    backend.enqueue_jobs(round_id, jobs)


def start_initial_stage(
    round_id: str, *, backend: CompetitionBackend,
    challengers: list[Mapping[str, Any]], baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """Start the week-one or first staged run after duplicate selection."""
    round_record = backend.get_round(round_id)
    sample = backend.list_sample(round_id)
    stages = stage_plan(
        challenger_count=len(challengers),
        week_one=round_record["incumbent_hotkey"] is None,
    )
    if not stages:
        raise ValueError("no qualified distinct challenger to evaluate")
    stage = stages[0]
    if stage not in {row["stage"] for row in sample}:
        raise ValueError("released sample does not contain the required first stage")
    role = "incumbent" if round_record["incumbent_hotkey"] else "reference"
    entries = [{
        "hotkey": str(baseline["hotkey"]),
        "package_sha256": str(baseline["package_sha256"]),
        "team_id": str(baseline["team_id"]),
        "role": role, "position": 0, "paired_gain": None,
    }]
    if entries[0]["package_sha256"] != round_record["baseline_package_sha256"]:
        raise ValueError("baseline package differs from frozen round")
    for position, challenger in enumerate(
        sorted(challengers, key=lambda row: (int(row["commit_block"]), str(row["hotkey"]))), start=1,
    ):
        entries.append({
            "hotkey": str(challenger["hotkey"]),
            "package_sha256": str(challenger["package_sha256"]),
            "team_id": str(challenger["team_id"]),
            "role": "challenger", "position": position, "paired_gain": None,
        })
    backend.start_stage(round_id, stage, entries)
    receipt = {"stage": stage, "entries": entries}
    _enqueue_current_stage(round_id, backend=backend, round_record=round_record,
                           receipt=receipt, sample=sample)
    return receipt


def advance_ready_stage(round_id: str, *, backend: CompetitionBackend) -> dict[str, Any]:
    """Re-enqueue current jobs, then advance once every paired score exists.

    Repeated calls after a crash are safe: stage selection and job identities
    are immutable at the backend. An incomplete score grid returns a waiting
    result instead of choosing winners from partial evidence.
    """
    round_record = backend.get_round(round_id)
    ledger = backend.list_stages(round_id)
    if not ledger:
        raise ValueError("round has no started stage")
    current = ledger[-1]
    stage = current["stage"]
    sample = backend.list_sample(round_id)
    _enqueue_current_stage(round_id, backend=backend, round_record=round_record,
                           receipt=current, sample=sample)
    if stage in {"final", "week_one"}:
        return {"stage": stage, "status": "final_running"}
    if stage not in {"prescreen", "screen"}:
        raise ValueError("unexpected active stage")
    paper_ids = [row["paper_id"] for row in sample if row["stage"] == stage]
    entries = current["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("active stage has no unique baseline")
    expected = {(paper_id, row["hotkey"]) for paper_id in paper_ids for row in entries}
    scores = backend.list_stage_scores(round_id, stage)
    grid: dict[tuple[str, str], dict[str, Any]] = {}
    package_by_hotkey = {row["hotkey"]: row["package_sha256"] for row in entries}
    for row in scores:
        identity = (row["paper_id"], row["hotkey"])
        if (identity not in expected or identity in grid or row["stage"] != stage
            or row["package_sha256"] != package_by_hotkey[row["hotkey"]]):
            raise ValueError("stage score grid differs from frozen entrants and sample")
        grid[identity] = row
    if len(grid) != len(expected):
        return {"stage": stage, "status": "waiting_for_scores",
                "scored": len(grid), "expected": len(expected)}
    infrastructure_faults = {
        paper_id for paper_id in paper_ids
        if any(grid[(paper_id, row["hotkey"])]["fault_kind"] == "infrastructure" for row in entries)
    }
    commits = {
        row["hotkey"]: int(row["commit_block"])
        for row in backend.list_submissions(round_id)
        if row["status"] == "accepted" and row["commit_block"] is not None
    }
    candidates: list[StageCandidate] = []
    for entry in entries:
        if entry["role"] != "challenger":
            continue
        hotkey = entry["hotkey"]
        if hotkey not in commits:
            raise ValueError("challenger commitment is missing from frozen intake")
        result = paired_result([
            PaperPairScore(
                paper_id,
                grid[(paper_id, hotkey)]["score"],
                grid[(paper_id, baseline[0]["hotkey"])]["score"],
                infrastructure_fault=paper_id in infrastructure_faults,
            ) for paper_id in paper_ids
        ])
        candidates.append(StageCandidate(
            hotkey, entry["team_id"], result.mean_gain, commits[hotkey],
        ))
    winners = advance(candidates, stage=stage)
    next_stage = "screen" if stage == "prescreen" else "final"
    if next_stage not in {row["stage"] for row in sample}:
        raise ValueError("released sample is missing the next stage")
    by_hotkey = {row["hotkey"]: row for row in entries}
    next_entries = [{**baseline[0], "position": 0, "paired_gain": None}]
    for position, winner in enumerate(winners, start=1):
        next_entries.append({
            **{key: by_hotkey[winner.hotkey][key]
               for key in ("hotkey", "package_sha256", "team_id", "role")},
            "position": position, "paired_gain": winner.paired_gain,
        })
    backend.start_stage(round_id, next_stage, next_entries)
    receipt = {"stage": next_stage, "entries": next_entries}
    _enqueue_current_stage(round_id, backend=backend, round_record=round_record,
                           receipt=receipt, sample=sample)
    return {"stage": next_stage, "status": "started", "selected_hotkeys": [
        winner.hotkey for winner in winners
    ]}
