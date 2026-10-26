"""Restartable scoring step for an independent validator's local replay."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .failure_policy import ReferenceFallbackRequired, incumbent_failure_summary
from .paper_score_controller import score_ready_paper
from .stage_controller import advance_ready_stage, start_failed_incumbent_reference_rerun


def tick_replay(
    round_id: str, *, backend: Any,
    bronze_for_paper: Callable[[str, str], Mapping[str, Any]],
    evaluate_for_stage: Callable[[str], Callable[..., Any]],
    reference_for_round: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score completed local jobs and select the next stage from local pairs.

    Worker processes claim jobs separately. A crash after a paper evaluation is
    frozen is safe: the scorer replays that receipt into any missing score rows.
    This step stops at a complete final grid for independent Gold and audit.
    """
    ledger = backend.list_stages(round_id)
    if not ledger:
        raise ValueError("replay has no stage receipt")
    current = ledger[-1]
    stage = current["stage"]
    if stage not in {"prescreen", "screen", "final", "week_one", "rerun"}:
        raise ValueError("replay has an unsupported active stage")
    source_stage = current.get("source_stage") if stage == "rerun" else stage
    if source_stage not in {"prescreen", "screen", "final", "week_one"}:
        raise ValueError("replay has an unsupported source stage")
    entrants = {row["hotkey"] for row in current["entries"]}
    challengers = {row["hotkey"] for row in current["entries"]
                   if row["role"] == "challenger"}
    if (not challengers or len(entrants) != len(current["entries"])
        or len(entrants) != len(challengers) + 1):
        raise ValueError("replay stage has invalid entrants")
    papers = [row for row in backend.list_sample(round_id) if row["stage"] == stage]
    paper_ids = [row["paper_id"] for row in papers]
    if not paper_ids or len(set(paper_ids)) != len(paper_ids):
        raise ValueError("replay stage has an invalid paper sample")
    expected = {(paper_id, hotkey) for paper_id in paper_ids for hotkey in challengers}
    pairs = backend.list_stage_pairs(round_id, stage)
    scored = {(row["paper_id"], row["challenger_hotkey"]) for row in pairs}
    if len(scored) != len(pairs) or not scored <= expected:
        raise ValueError("replay pair grid differs from frozen stage")
    attempted = 0
    for paper_id in paper_ids:
        if all((paper_id, hotkey) in scored for hotkey in challengers):
            continue
        jobs = backend.list_paper_jobs(round_id, stage, paper_id)
        if (len(jobs) != len(entrants)
            or {row["hotkey"] for row in jobs} != entrants
            or any(row["status"] != "completed" for row in jobs)):
            continue
        score_ready_paper(
            round_id, stage, paper_id, backend=backend,
            bronze_artifact=bronze_for_paper(stage, paper_id),
            evaluate=evaluate_for_stage(source_stage),
        )
        attempted += 1
    if source_stage in {"prescreen", "screen"}:
        try:
            result = advance_ready_stage(round_id, backend=backend)
        except ReferenceFallbackRequired as exc:
            if reference_for_round is None:
                raise ValueError("replay reference receipt is required for incumbent failure") from exc
            result = start_failed_incumbent_reference_rerun(
                round_id, backend=backend, failure=exc.summary,
                reference=reference_for_round(round_id),
            )
        return {**result, "scored_papers_this_tick": attempted}
    final_pairs = backend.list_stage_pairs(round_id, stage)
    final_grid = {(row["paper_id"], row["challenger_hotkey"]) for row in final_pairs}
    if final_grid != expected:
        return {"stage": stage, "status": "waiting_for_scores",
                "scored": len(final_grid), "expected": len(expected),
                "scored_papers_this_tick": attempted}
    baseline = [row for row in current["entries"] if row["role"] == "incumbent"]
    if baseline:
        excluded = {row["paper_id"] for row in final_pairs if row["infrastructure_fault"]}
        failure = incumbent_failure_summary(
            round_id, stage=stage, paper_ids=paper_ids,
            excluded_infrastructure_papers=excluded,
            incumbent_hotkey=baseline[0]["hotkey"], backend=backend,
        )
        if failure.reference_required:
            if reference_for_round is None:
                raise ValueError("replay reference receipt is required for incumbent failure")
            result = start_failed_incumbent_reference_rerun(
                round_id, backend=backend, failure=failure,
                reference=reference_for_round(round_id),
            )
            return {**result, "scored_papers_this_tick": attempted}
    return {"stage": stage, "status": "ready_for_independent_final_review",
            "scored": len(final_grid), "expected": len(expected),
            "scored_papers_this_tick": attempted}
