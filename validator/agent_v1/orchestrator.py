from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationDecision
from .adjudication_runner import AdjudicationPass, run_adjudication_case
from .batch_scoring import BatchScoreResult, score_batch
from .bronze_diff import compare_miner_to_bronze_result
from .comparison_models import BronzeDiffCase, ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from .pairing import RelationClassifier
from .record_projection import project_agent_artifact
from .relation_classifier import classify_candidate_pair
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
    relation_classifier: RelationClassifier | None = None,
    source_context: str = "",
    source_context_by_span_id: dict[str, str] | None = None,
    adjudication_max_workers: int = 4,
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

    comparison_results = [
        compare_miner_to_bronze_result(
            paper_id=paper_id,
            miner_id=submission.miner_id,
            bronze_candidates=bronze_candidates,
            miner_candidates=submission.candidates,
            relation_classifier=relation_classifier,
        )
        for submission in miner_submissions
    ]
    diff_cases = _dedupe_cases(
        case
        for result in comparison_results
        for case in result.cases
    )
    comparison_equivalent_candidate_groups = _dedupe_equivalent_candidate_groups(
        group
        for result in comparison_results
        for group in result.equivalent_candidate_groups
    )

    def adjudicate(case: BronzeDiffCase) -> tuple[AdjudicationConsensus, AdjudicationDecision | None]:
        candidates = [candidates_by_id[candidate_id] for candidate_id in case.candidate_ids if candidate_id in candidates_by_id]
        context = AdjudicationContextBundle(
            case=case,
            candidates=candidates,
            source_context=_source_context_for_candidates(
                candidates,
                source_context_by_span_id or {},
                fallback=source_context,
            ),
            candidate_order_seed=case.case_id,
        )
        consensus = run_adjudication_case(
            context,
            passes=adjudication_passes,
            tiebreak_pass=tiebreak_pass,
            direct_judge_confidence=direct_judge_confidence,
        )
        decision = _decision_from_consensus(case, consensus, context.candidates)
        return consensus, decision

    worker_count = max(1, min(int(adjudication_max_workers or 1), len(diff_cases) or 1))
    if worker_count == 1:
        adjudicated = [adjudicate(case) for case in diff_cases]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            adjudicated = list(executor.map(adjudicate, diff_cases))
    consensus_records = [consensus for consensus, _decision in adjudicated]
    decisions = [decision for _consensus, decision in adjudicated if decision is not None]
    candidate_ids_for_equivalence = _candidate_ids_retained_for_silver(
        candidate_pool,
        decisions,
        comparison_equivalent_candidate_groups,
    )
    semantic_equivalent_candidate_groups = _semantic_equivalence_groups(
        [candidate for candidate in candidate_pool if candidate.candidate_id in candidate_ids_for_equivalence],
        relation_classifier=relation_classifier,
        existing_groups=comparison_equivalent_candidate_groups,
    )
    equivalent_candidate_groups = _dedupe_equivalent_candidate_groups(
        [*comparison_equivalent_candidate_groups, *semantic_equivalent_candidate_groups]
    )

    silver_record = build_silver_record(
        paper_id=paper_id,
        silver_record_id=silver_record_id,
        bronze_record_id=bronze_record_id,
        candidates=candidate_pool,
        decisions=decisions,
        equivalent_candidate_groups=equivalent_candidate_groups,
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
    by_key: dict[tuple[str, str, tuple[str, ...]], BronzeDiffCase] = {}
    for case in cases:
        key = (case.miner_id, case.mismatch_type, tuple(sorted(case.candidate_ids)))
        if key not in by_key:
            by_key[key] = case
    return list(by_key.values())


def _dedupe_equivalent_candidate_groups(groups: Iterable[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    deduped: list[list[str]] = []
    for group in groups:
        key = tuple(sorted(candidate_id for candidate_id in group if candidate_id))
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        deduped.append(list(key))
    return deduped


def _candidate_ids_retained_for_silver(
    candidates: list[ComparisonCandidate],
    decisions: list[AdjudicationDecision],
    comparison_equivalent_candidate_groups: list[list[str]],
) -> set[str]:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    accepted_bronze_ids = {
        candidate_id
        for decision in decisions
        for candidate_id in decision.accepted_candidate_ids
        if (candidate := candidates_by_id.get(candidate_id)) and candidate.origin == "bronze"
    }
    rejected_bronze_ids = {
        candidate_id
        for decision in decisions
        for candidate_id in decision.rejected_candidate_ids
        if (candidate := candidates_by_id.get(candidate_id)) and candidate.origin == "bronze"
    }
    retained = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.origin == "bronze"
        and (candidate.candidate_id not in rejected_bronze_ids or candidate.candidate_id in accepted_bronze_ids)
    }
    for decision in decisions:
        retained.update(candidate_id for candidate_id in decision.accepted_candidate_ids if candidate_id in candidates_by_id)
        retained.update(candidate_id for candidate_id in decision.valid_alternative_candidate_ids if candidate_id in candidates_by_id)
    for group in comparison_equivalent_candidate_groups:
        retained.update(candidate_id for candidate_id in group if candidate_id in candidates_by_id)
    return retained


def _semantic_equivalence_groups(
    candidates: list[ComparisonCandidate],
    *,
    relation_classifier: RelationClassifier | None,
    existing_groups: list[list[str]],
    min_confidence: float = 0.72,
) -> list[list[str]]:
    if len(candidates) < 2:
        return []
    classifier = relation_classifier or classify_candidate_pair
    existing_pairs = _candidate_pairs_from_groups(existing_groups)
    groups: list[list[str]] = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            pair_key = tuple(sorted((left.candidate_id, right.candidate_id)))
            if pair_key in existing_pairs or not _likely_same_silver_unit(left, right):
                continue
            try:
                edge = classifier(left, right)
            except Exception:
                continue
            if edge.relation == "semantic_equivalent" and edge.confidence >= min_confidence:
                groups.append([left.candidate_id, right.candidate_id])
    return groups


def _candidate_pairs_from_groups(groups: list[list[str]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for group in groups:
        ids = sorted(candidate_id for candidate_id in group if candidate_id)
        for index, left in enumerate(ids):
            for right in ids[index + 1 :]:
                pairs.add((left, right))
    return pairs


def _likely_same_silver_unit(left: ComparisonCandidate, right: ComparisonCandidate) -> bool:
    if left.candidate_id == right.candidate_id:
        return False
    if left.normalized_statement == right.normalized_statement:
        return True
    if set(left.source_span_ids).intersection(right.source_span_ids):
        return True
    left_tokens = _meaningful_tokens(left.normalized_statement)
    right_tokens = _meaningful_tokens(right.normalized_statement)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens.intersection(right_tokens))
    return overlap / max(1, min(len(left_tokens), len(right_tokens))) >= 0.35


def _meaningful_tokens(value: str) -> set[str]:
    return {token for token in value.split() if len(token) > 3}


def _source_context_for_candidates(
    candidates: list[ComparisonCandidate],
    source_context_by_span_id: dict[str, str],
    *,
    fallback: str = "",
) -> str:
    if not source_context_by_span_id:
        return fallback
    ordered_span_ids: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        for span_id in candidate.source_span_ids:
            if span_id and span_id not in seen:
                ordered_span_ids.append(span_id)
                seen.add(span_id)
    lines: list[str] = []
    for span_id in ordered_span_ids:
        text = source_context_by_span_id.get(span_id)
        if text is None:
            text = source_context_by_span_id.get(_alternate_page_span_id(span_id))
        if text:
            lines.append(f"{span_id}: {text.strip()}")
    if lines:
        return "\n".join(lines)
    return fallback


def _alternate_page_span_id(span_id: str) -> str:
    if span_id.endswith("-markdown"):
        return f"{span_id[: -len('-markdown')]}-001"
    if span_id.endswith("-001"):
        return f"{span_id[: -len('-001')]}-markdown"
    return ""


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
