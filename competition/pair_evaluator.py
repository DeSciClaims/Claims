"""Run canonical Silver once, then score each challenger against its own pair."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from validator.agent_v1.adjudication_runner import AdjudicationPass
from validator.agent_v1.orchestrator import (
    MinerArtifactSubmission,
    PaperSilverPipelineResult,
    run_paper_silver_pipeline,
)

from .scoring import score_pairwise_paper


@dataclass(frozen=True)
class WeeklyPaperEvaluation:
    pipeline: PaperSilverPipelineResult
    pair_rows: tuple[dict[str, Any], ...]


def evaluate_weekly_paper(
    *, paper_id: str, bronze_artifact: dict[str, Any],
    artifacts_by_hotkey: Mapping[str, dict[str, Any] | None],
    baseline_hotkey: str, challenger_hotkeys: list[str],
    silver_record_id: str, adjudication_passes: list[AdjudicationPass],
    tiebreak_pass: AdjudicationPass | None = None,
    judge_id: str = "J1", stage: str = "final",
    source_context_by_span_id: dict[str, str] | None = None,
) -> WeeklyPaperEvaluation:
    """Produce a common Silver record and pair-specific scores for one PDF.

    The trusted controller runs this after every worker result for the paper
    has arrived. A missing package output is scored zero for that package.
    Judge order alternates with the released J1/J2 assignment.
    """
    if stage not in {"prescreen", "screen", "final", "week_one"}:
        raise ValueError("unknown competition stage")
    if judge_id not in {"J1", "J2"} or len(adjudication_passes) != 2:
        raise ValueError("two frozen judges and a J1/J2 assignment are required")
    if (not baseline_hotkey or not challenger_hotkeys
        or baseline_hotkey in challenger_hotkeys
        or len(challenger_hotkeys) != len(set(challenger_hotkeys))):
        raise ValueError("paper needs a baseline and distinct challengers")
    expected_hotkeys = {baseline_hotkey, *challenger_hotkeys}
    if set(artifacts_by_hotkey) != expected_hotkeys:
        raise ValueError("paper artifacts differ from selected stage entrants")
    for artifact in artifacts_by_hotkey.values():
        if artifact is not None and artifact.get("paper", {}).get("paper_id") != paper_id:
            raise ValueError("artifact belongs to another paper")
    passes = adjudication_passes if judge_id == "J1" else list(reversed(adjudication_passes))
    pipeline = run_paper_silver_pipeline(
        paper_id=paper_id, bronze_artifact=bronze_artifact,
        miner_artifacts=[MinerArtifactSubmission(miner_id=hotkey, artifact=artifact)
                         for hotkey, artifact in artifacts_by_hotkey.items() if artifact is not None],
        silver_record_id=silver_record_id, adjudication_passes=passes,
        tiebreak_pass=tiebreak_pass,
        source_context_by_span_id=source_context_by_span_id,
    )
    candidates = {row.miner_id: row.candidates for row in pipeline.miner_submissions}
    findings = {row.miner_id: row.normal_findings for row in pipeline.miner_submissions}
    pairs: list[dict[str, Any]] = []
    for hotkey in challenger_hotkeys:
        result = score_pairwise_paper(
            challenger_id=hotkey, incumbent_id=baseline_hotkey,
            silver_record=pipeline.silver_record,
            challenger_candidates=candidates.get(hotkey, []),
            incumbent_candidates=candidates.get(baseline_hotkey, []),
            challenger_findings=findings.get(hotkey),
            incumbent_findings=findings.get(baseline_hotkey),
        )
        pairs.append({
            "paper_id": paper_id, "challenger_hotkey": hotkey,
            "baseline_hotkey": baseline_hotkey,
            "challenger_score": result.challenger.score if artifacts_by_hotkey[hotkey] is not None else 0.0,
            "baseline_score": result.incumbent.score if artifacts_by_hotkey[baseline_hotkey] is not None else 0.0,
            "infrastructure_fault": False,
            "details_public": {
                "silver_record_id": pipeline.silver_record.silver_record_id,
                "challenger_covered_units": len(result.challenger.covered_required_silver_units),
                "baseline_covered_units": len(result.incumbent.covered_required_silver_units),
            },
        })
    return WeeklyPaperEvaluation(pipeline, tuple(pairs))
