from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationDecision
from .adjudication_runner import AdjudicationPass, run_adjudication_case
from .batch_scoring import BatchScoreResult, score_batch
from .bronze_diff import compare_miner_to_bronze
from .comparison_models import BronzeDiffCase, ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from .record_projection import project_agent_artifact
from .silver_builder import build_silver_record
from .silver_scoring import score_miner_against_silver


@dataclass(frozen=True)
class MinerPaperSubmission:
    miner_id: str
    paper_id: str
    candidates: list[ComparisonCandidate]


@dataclass(frozen=True)
class SilverScoringJob:
    submission: MinerPaperSubmission
    silver_record: SilverRecord


@dataclass(frozen=True)
class MinerArtifactSubmission:
    miner_id: str
    artifact: dict


@dataclass(frozen=True)
class PaperSilverPipelineResult:
    paper_id: str
    bronze_candidates: list[ComparisonCandidate]
    miner_submissions: list[MinerPaperSubmission]
    diff_cases: list[BronzeDiffCase]
    adjudication_consensus: list[AdjudicationConsensus]
    adjudication_decisions: list[AdjudicationDecision]
    silver_record: SilverRecord
    scores: list[SilverScoreBreakdown]


def run_paper_silver_pipeline(
    *,
    paper_id: str,
    bronze_artifact: dict,
    miner_artifacts: list[MinerArtifactSubmission],
    silver_record_id: str,
    bronze_record_id: str | None = None,
    adjudication_passes: list[AdjudicationPass],
    tiebreak_pass: AdjudicationPass | None = None,
    direct_judge_confidence: float = 0.9,
    source_context: str = "",
) -> PaperSilverPipelineResult:
    bronze_candidates = project_agent_artifact(bronze_artifact, origin="bronze")
    miner_submissions = [
        MinerPaperSubmission(
            miner_id=submission.miner_id,
            paper_id=paper_id,
            candidates=project_agent_artifact(submission.artifact, origin="miner", miner_id=submission.miner_id),
        )
        for submission in miner_artifacts
    ]
    candidate_pool = [*bronze_candidates, *[candidate for submission in miner_submissions for candidate in submission.candidates]]
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidate_pool}

    diff_cases = _dedupe_cases(
        case
        for submission in miner_submissions
        for case in compare_miner_to_bronze(
            paper_id=paper_id,
            miner_id=submission.miner_id,
            bronze_candidates=bronze_candidates,
            miner_candidates=submission.candidates,
        )
    )
    consensus_records: list[AdjudicationConsensus] = []
    decisions: list[AdjudicationDecision] = []
    for case in diff_cases:
        context = AdjudicationContextBundle(
            case=case,
            candidates=[candidates_by_id[candidate_id] for candidate_id in case.candidate_ids if candidate_id in candidates_by_id],
            source_context=source_context,
            candidate_order_seed=case.case_id,
        )
        consensus = run_adjudication_case(
            context,
            passes=adjudication_passes,
            tiebreak_pass=tiebreak_pass,
            direct_judge_confidence=direct_judge_confidence,
        )
        consensus_records.append(consensus)
        decision = _decision_from_consensus(case, consensus, context.candidates)
        if decision:
            decisions.append(decision)

    silver_record = build_silver_record(
        paper_id=paper_id,
        silver_record_id=silver_record_id,
        bronze_record_id=bronze_record_id,
        candidates=candidate_pool,
        decisions=decisions,
    )
    scores = score_silver_jobs_parallel(
        [SilverScoringJob(submission=submission, silver_record=silver_record) for submission in miner_submissions]
    )
    return PaperSilverPipelineResult(
        paper_id=paper_id,
        bronze_candidates=bronze_candidates,
        miner_submissions=miner_submissions,
        diff_cases=diff_cases,
        adjudication_consensus=consensus_records,
        adjudication_decisions=decisions,
        silver_record=silver_record,
        scores=scores,
    )


def score_silver_jobs_parallel(
    jobs: list[SilverScoringJob],
    *,
    max_workers: int = 8,
) -> list[SilverScoreBreakdown]:
    if not jobs:
        return []
    worker_count = max(1, min(max_workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(_score_job, jobs))


def run_batch_silver_scoring(
    *,
    batch_id: str,
    jobs: list[SilverScoringJob],
    gate_threshold: float = 0.5,
    max_workers: int = 8,
) -> BatchScoreResult:
    paper_scores = score_silver_jobs_parallel(jobs, max_workers=max_workers)
    return score_batch(batch_id=batch_id, paper_scores=paper_scores, gate_threshold=gate_threshold)


def _score_job(job: SilverScoringJob) -> SilverScoreBreakdown:
    return score_miner_against_silver(
        miner_id=job.submission.miner_id,
        miner_candidates=job.submission.candidates,
        silver_record=job.silver_record,
    )


def _dedupe_cases(cases: Iterable[BronzeDiffCase]) -> list[BronzeDiffCase]:
    by_key: dict[tuple[str, tuple[str, ...]], BronzeDiffCase] = {}
    for case in cases:
        key = (case.mismatch_type, tuple(sorted(case.candidate_ids)))
        if key not in by_key:
            by_key[key] = case
    return list(by_key.values())


def _decision_from_consensus(
    case: BronzeDiffCase,
    consensus: AdjudicationConsensus,
    candidates: list[ComparisonCandidate],
) -> AdjudicationDecision | None:
    if consensus.final_disposition is None:
        return None
    bronze_ids = [candidate.candidate_id for candidate in candidates if candidate.origin == "bronze"]
    miner_ids = [candidate.candidate_id for candidate in candidates if candidate.origin == "miner"]
    disposition = consensus.final_disposition
    if case.mismatch_type == "MISSING_FROM_MINER" and bronze_ids and not miner_ids:
        if disposition in {"accepted_improvement", "benign_difference", "both_valid"}:
            disposition = "miner_error"
        elif disposition == "reference_error":
            return AdjudicationDecision(
                case_id=case.case_id,
                disposition=disposition,
                rejected_candidate_ids=bronze_ids,
                creates_required_silver_unit=False,
                importance=_case_importance(candidates),
                rationale=consensus.rationale,
                consensus=consensus,
            )

    if disposition == "reference_error":
        return AdjudicationDecision(
            case_id=case.case_id,
            disposition=disposition,
            accepted_candidate_ids=miner_ids,
            rejected_candidate_ids=bronze_ids,
            importance=_case_importance(candidates),
            rationale=consensus.rationale,
            consensus=consensus,
        )
    if disposition in {"accepted_improvement", "benign_difference", "both_valid"}:
        accepted = [*bronze_ids, *miner_ids] if bronze_ids else miner_ids
        return AdjudicationDecision(
            case_id=case.case_id,
            disposition=disposition,
            accepted_candidate_ids=accepted,
            creates_required_silver_unit=case.mismatch_type != "EXTRA_FROM_MINER",
            creates_optional_improvement_unit=case.mismatch_type == "EXTRA_FROM_MINER",
            importance=_case_importance(candidates),
            rationale=consensus.rationale,
            consensus=consensus,
        )
    if disposition == "miner_error":
        return AdjudicationDecision(
            case_id=case.case_id,
            disposition=disposition,
            accepted_candidate_ids=bronze_ids,
            rejected_candidate_ids=miner_ids,
            importance=_case_importance(candidates),
            rationale=consensus.rationale,
            consensus=consensus,
        )
    if disposition == "both_invalid":
        return AdjudicationDecision(
            case_id=case.case_id,
            disposition=disposition,
            rejected_candidate_ids=[candidate.candidate_id for candidate in candidates],
            importance=_case_importance(candidates),
            rationale=consensus.rationale,
            consensus=consensus,
        )
    return None


def _case_importance(candidates: list[ComparisonCandidate]):
    if any(candidate.importance == "central" for candidate in candidates):
        return "central"
    if any(candidate.importance == "supporting" for candidate in candidates):
        return "supporting"
    return "minor"
