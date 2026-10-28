"""Run canonical Silver once, then score each challenger against its own pair."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from validator.agent_v1.adjudication_models import AdjudicationConsensus, AdjudicationContextBundle
from validator.agent_v1.adjudication_runner import AdjudicationPass
from validator.agent_v1.orchestrator import (
    MinerArtifactSubmission,
    PaperSilverPipelineResult,
    run_paper_silver_pipeline,
)

from .scoring import score_pairwise_paper
from .silver_runtime import silver_pipeline_kwargs, silver_runtime_config


@dataclass(frozen=True)
class WeeklyPaperEvaluation:
    pipeline: PaperSilverPipelineResult
    pair_rows: tuple[dict[str, Any], ...]
    judge_usage_events: tuple[dict[str, Any], ...] | None = None


def evaluate_weekly_paper(
    *, paper_id: str, bronze_artifact: dict[str, Any],
    artifacts_by_hotkey: Mapping[str, dict[str, Any] | None],
    baseline_hotkey: str, challenger_hotkeys: list[str],
    silver_record_id: str,
    adjudication_passes_by_judge: Mapping[str, tuple[AdjudicationPass, AdjudicationPass]],
    tiebreak_pass: AdjudicationPass | None = None,
    judge_id: str = "J1", stage: str = "final",
    source_context_by_span_id: dict[str, str] | None = None,
    decision_router: Callable[[AdjudicationContextBundle], AdjudicationConsensus | None] | None = None,
) -> WeeklyPaperEvaluation:
    """Produce a common Silver record and pair-specific scores for one PDF.

    The trusted controller runs this after every worker result for the paper
    has arrived. A missing package output is scored zero for that package.
    The released assignment selects one frozen two-pass judge configuration.
    Its own primary pair can split and invoke the common tiebreak pass.
    """
    if stage not in {"prescreen", "screen", "final", "week_one"}:
        raise ValueError("unknown competition stage")
    if (judge_id not in {"J1", "J2", "routing"}
        or set(adjudication_passes_by_judge) != {"J1", "J2"}
        or any(len(passes) != 2 for passes in adjudication_passes_by_judge.values())
        or (judge_id == "routing" and (stage != "prescreen" or decision_router is None))
        or (stage in {"final", "week_one"} and decision_router is not None)):
        raise ValueError("two frozen judge configurations and a valid assignment are required")
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
    # The pre-screen has no seeded judge assignment. Its uncertain cases use
    # the frozen J1 configuration; screen/final/week-one use their assignment.
    passes = list(adjudication_passes_by_judge["J1" if judge_id == "routing" else judge_id])
    silver_settings = silver_pipeline_kwargs(silver_runtime_config()["settings"])
    pipeline = run_paper_silver_pipeline(
        paper_id=paper_id, bronze_artifact=bronze_artifact,
        miner_artifacts=[MinerArtifactSubmission(miner_id=hotkey, artifact=artifact)
                         for hotkey, artifact in artifacts_by_hotkey.items() if artifact is not None],
        silver_record_id=silver_record_id, adjudication_passes=passes,
        tiebreak_pass=tiebreak_pass,
        source_context_by_span_id=source_context_by_span_id,
        decision_router=decision_router,
        **silver_settings,
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
            equivalence_shared_coverage=bool(silver_settings["equivalence_shared_coverage"]),
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
