"""Resume a staged weekly evaluation from durable backend receipts."""

from __future__ import annotations

from typing import Any, Mapping

from .intake_lock import CompetitionBackend
from .failure_policy import (
    IncumbentFailureSummary, ReferenceFallbackRequired, incumbent_failure_summary,
)
from .jobs import build_jobs
from .policy import PaperPairScore, paired_result
from .reference_rerun import reference_rerun_plan
from .stages import StageCandidate, advance
from .round_evaluation import round_evaluation, stages_for_round


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
    stages = stages_for_round(round_record, len(challengers))
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
    return {"status": "started", **receipt}


def start_failed_incumbent_reference_rerun(
    round_id: str, *, backend: CompetitionBackend,
    failure: IncumbentFailureSummary, reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist and enqueue the precommitted reference on the failed paper grid."""
    round_record = backend.get_round(round_id)
    ledger = backend.list_stages(round_id)
    if not ledger:
        raise ValueError("reference rerun has no source stage")
    sample = backend.list_sample(round_id)
    plan = reference_rerun_plan(
        failure=failure, round_record=round_record,
        source_receipt=ledger[-1], sample=sample, reference=reference,
    )
    started = backend.start_reference_rerun(round_id, reference)
    if started["stage"] != "rerun" or started["source_stage"] != plan.source_stage:
        raise ValueError("stored reference rerun differs from failed stage")
    frozen = backend.list_stages(round_id)[-1]
    if (frozen["stage"] != "rerun"
        or any(frozen["entries"][index][key] != entry[key]
               for index, entry in enumerate(plan.entries)
               for key in ("hotkey", "package_sha256", "team_id", "role", "position"))
        or len(frozen["entries"]) != len(plan.entries)):
        raise ValueError("stored reference rerun entrants differ from plan")
    frozen_sample = [row for row in backend.list_sample(round_id) if row["stage"] == "rerun"]
    if (len(frozen_sample) != len(plan.papers)
        or any(frozen_sample[index][key] != paper[key]
               for index, paper in enumerate(plan.papers)
               for key in ("stage", "paper_id", "position", "judge_id", "source_sha256"))):
        raise ValueError("stored reference rerun sample differs from failed stage")
    _enqueue_current_stage(round_id, backend=backend, round_record=round_record,
                           receipt=frozen, sample=backend.list_sample(round_id))
    return {"status": "reference_rerun_started", "stage": "rerun",
            "source_stage": plan.source_stage,
            "failed_papers": failure.failed_papers,
            "eligible_papers": failure.eligible_papers}


def advance_ready_stage(round_id: str, *, backend: CompetitionBackend, identity_preparer=None) -> dict[str, Any]:
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
    source_stage = current.get("source_stage") if stage == "rerun" else stage
    if stage in {"final", "week_one"} or source_stage == "final":
        return {"stage": stage, "status": "final_running"}
    if source_stage not in {"prescreen", "screen"}:
        raise ValueError("unexpected active stage")
    paper_ids = [row["paper_id"] for row in sample if row["stage"] == stage]
    entries = current["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("active stage has no unique baseline")
    challengers = [entry for entry in entries if entry["role"] == "challenger"]
    expected = {(paper_id, row["hotkey"]) for paper_id in paper_ids for row in challengers}
    pairs = backend.list_stage_pairs(round_id, stage)
    grid: dict[tuple[str, str], dict[str, Any]] = {}
    package_by_hotkey = {row["hotkey"]: row["package_sha256"] for row in challengers}
    for row in pairs:
        identity = (row["paper_id"], row["challenger_hotkey"])
        if (identity not in expected or identity in grid or row["stage"] != stage
            or row["challenger_package_sha256"] != package_by_hotkey[row["challenger_hotkey"]]
            or row["baseline_hotkey"] != baseline[0]["hotkey"]
            or row["baseline_package_sha256"] != baseline[0]["package_sha256"]):
            raise ValueError("stage pair grid differs from frozen entrants and sample")
        grid[identity] = row
    if len(grid) != len(expected):
        return {"stage": stage, "status": "waiting_for_scores",
                "scored": len(grid), "expected": len(expected)}
    if round_record.get("rules_public", {}).get("policy", {}).get("team_identity") == "linked":
        if identity_preparer is None:
            raise ValueError("advancement needs a team identity resolver")
        identity = identity_preparer(round_id)
        if identity.get("status") == "waiting_for_team_review":
            return identity
        entries = [{**entry, "team_id": identity["team_by_hotkey"].get(entry["hotkey"], entry["team_id"])} for entry in entries]
        baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    infrastructure_faults = {
        paper_id for paper_id in paper_ids
        if any(grid[(paper_id, row["hotkey"])]["infrastructure_fault"] for row in challengers)
    }
    if paper_ids and infrastructure_faults == set(paper_ids):
        return {"stage": stage, "status": "infrastructure_sample_exhausted",
                "excluded_papers": len(paper_ids)}
    if baseline[0]["role"] == "incumbent":
        failure = incumbent_failure_summary(
            round_id, stage=stage, paper_ids=paper_ids,
            excluded_infrastructure_papers=infrastructure_faults,
            incumbent_hotkey=baseline[0]["hotkey"], backend=backend,
        )
        if failure.reference_required:
            raise ReferenceFallbackRequired(failure)
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
                grid[(paper_id, hotkey)]["challenger_score"],
                grid[(paper_id, hotkey)]["baseline_score"],
                infrastructure_fault=paper_id in infrastructure_faults,
            ) for paper_id in paper_ids
        ])
        candidates.append(StageCandidate(
            hotkey, entry["team_id"], result.mean_gain, commits[hotkey],
        ))
    evaluation = round_evaluation(round_record)
    if evaluation is None:
        winners = advance(candidates, stage=source_stage)
        next_stage = "screen" if source_stage == "prescreen" else "final"
    else:
        current_settings = evaluation.stage(source_stage)
        winners = advance(candidates, stage=source_stage,
                          limit=current_settings.advance_top,
                          per_team=current_settings.max_per_team)
        names = [item.name for item in evaluation.stages]
        next_stage = names[names.index(source_stage) + 1]
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
