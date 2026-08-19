from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationDecision, AdjudicationVote
from .adjudication_runner import AdjudicationPass, run_adjudication_cases
from .batch_scoring import BatchScoreResult, score_batch
from .comparison_models import BronzeDiffCase, CandidatePairEdge, ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from .file_agent_workflow import FileAgentSilverWorkflow, FileAgentWorkflowSession
from .models import AgentV1ValidationFinding
from .pairing import (
    RelationClassifier,
    bound_consolidation_pair_hits,
    classify_filtered_candidate_pairs,
    filter_candidate_pairs,
)
from .record_projection import project_agent_artifact
from .relation_classifier import classify_candidate_pair
from .silver_builder import build_silver_record
from .silver_importance import SilverImportanceClassifier, apply_silver_importance
from .silver_scoring import score_miner_against_silver


CONSOLIDATION_SEMANTIC_CONFIDENCE = 0.88
CONSOLIDATION_COMPATIBLE_CONFIDENCE = 0.72
SAME_UNIT_RELATIONS = {"semantic_equivalent", "compatible_refinement", "compatible_split_merge"}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinerPaperSubmission:
    miner_id: str
    paper_id: str
    candidates: list[ComparisonCandidate]
    normal_findings: list[AgentV1ValidationFinding] | None = None


@dataclass(frozen=True)
class SilverScoringJob:
    submission: MinerPaperSubmission
    silver_record: SilverRecord


@dataclass(frozen=True)
class MinerArtifactSubmission:
    miner_id: str
    artifact: dict
    claim_assessments: list[dict] | None = None


@dataclass(frozen=True)
class PaperSilverPipelineResult:
    paper_id: str
    bronze_candidates: list[ComparisonCandidate]
    miner_submissions: list[MinerPaperSubmission]
    candidate_graph_edges: list[CandidatePairEdge]
    diff_cases: list[BronzeDiffCase]
    adjudication_consensus: list[AdjudicationConsensus]
    adjudication_decisions: list[AdjudicationDecision]
    silver_record: SilverRecord
    scores: list[SilverScoreBreakdown]
    stage_timings: list[dict] = field(default_factory=list)


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
    consolidation_relation_classifier: RelationClassifier | None = None,
    importance_classifier: SilverImportanceClassifier | None = None,
    paper_context: dict | None = None,
    validation_findings_by_miner_id: dict[str, list[AgentV1ValidationFinding]] | None = None,
    source_context: str = "",
    source_context_by_span_id: dict[str, str] | None = None,
    adjudication_max_workers: int = 4,
    adjudication_batch_size: int = 8,
    max_eligible_claims_per_miner: int = 6,
    max_adjudication_cases: int = 80,
    adjudication_progress_sink: Callable[
        [list[AdjudicationContextBundle], list[AdjudicationVote]],
        None,
    ]
    | None = None,
    file_agent_workflow: FileAgentSilverWorkflow | None = None,
) -> PaperSilverPipelineResult:
    stage_timings: list[dict] = []

    projection_timer = _stage_start("silver_projection", "Silver projection")
    bronze_candidates = project_agent_artifact(bronze_artifact, origin="bronze")
    miner_submissions: list[MinerPaperSubmission] = []
    projected_miner_candidate_count = 0
    assessment_rejected_candidate_count = 0
    for submission in miner_artifacts:
        projected_candidates = project_agent_artifact(
            submission.artifact,
            origin="miner",
            miner_id=submission.miner_id,
        )
        projected_miner_candidate_count += len(projected_candidates)
        selected_candidates = _select_assessed_candidates(
            projected_candidates,
            submission.claim_assessments,
            max_claims=max_eligible_claims_per_miner,
        )
        assessment_rejected_candidate_count += len(projected_candidates) - len(selected_candidates)
        miner_submissions.append(
            MinerPaperSubmission(
                miner_id=submission.miner_id,
                paper_id=paper_id,
                candidates=selected_candidates,
                normal_findings=(validation_findings_by_miner_id or {}).get(
                    submission.miner_id, []
                ),
            )
        )
    miner_submissions, case_budget_rejected_candidate_count = _apply_case_budget(
        miner_submissions,
        bronze_candidate_count=len(bronze_candidates),
        max_adjudication_cases=max_adjudication_cases,
    )
    candidate_pool = [*bronze_candidates, *[candidate for submission in miner_submissions for candidate in submission.candidates]]
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidate_pool}
    workflow_fallbacks: list[str] = []
    file_session: FileAgentWorkflowSession | None = None
    if file_agent_workflow is not None:
        file_session = file_agent_workflow.start_session(
            paper_id=paper_id,
            workspace_id=silver_record_id,
            candidates=candidate_pool,
            paper_context=paper_context or _paper_context_from_artifact(bronze_artifact),
            source_context_by_span_id=source_context_by_span_id or {},
        )
    stage_timings.append(_stage_finish(
        projection_timer,
        paper_id=paper_id,
        metadata={
            "bronze_candidate_count": len(bronze_candidates),
            "miner_count": len(miner_submissions),
            "candidate_count": len(candidate_pool),
            "projected_miner_candidate_count": projected_miner_candidate_count,
            "assessment_rejected_candidate_count": assessment_rejected_candidate_count,
            "case_budget_rejected_candidate_count": case_budget_rejected_candidate_count,
            "max_eligible_claims_per_miner": max_eligible_claims_per_miner,
            "max_adjudication_cases": max_adjudication_cases,
        },
    ))

    comparison_timer = _stage_start("silver_comparison", "Comparison graph")
    comparison_hits: dict[str, list] = {}
    comparison_mode = "legacy"
    if file_session is not None:
        try:
            comparison_edges = file_session.run_comparison()
            comparison_mode = "file_agent"
        except Exception as exc:
            if not file_session.fallback_to_legacy:
                raise
            LOGGER.exception("File-agent comparison failed for paper=%s; using legacy comparison.", paper_id)
            workflow_fallbacks.append(f"comparison:{type(exc).__name__}")
            comparison_edges = []
    else:
        comparison_edges = []
    if comparison_mode == "legacy":
        comparison_hits = {
            submission.miner_id: filter_candidate_pairs(bronze_candidates, submission.candidates)
            for submission in miner_submissions
        }
        comparison_edges = classify_filtered_candidate_pairs(
            [hit for hits in comparison_hits.values() for hit in hits],
            relation_classifier=relation_classifier,
        )
    candidate_graph_edges = _dedupe_candidate_graph_edges(comparison_edges)
    diff_cases = _comparison_cases_from_graph(
        paper_id=paper_id,
        bronze_candidates=bronze_candidates,
        miner_submissions=miner_submissions,
        candidate_graph_edges=candidate_graph_edges,
    )
    if len(diff_cases) > max_adjudication_cases:
        raise RuntimeError(
            "Comparison produced more adjudication cases than the configured paper ceiling: "
            f"cases={len(diff_cases)} ceiling={max_adjudication_cases}."
        )
    if file_session is not None:
        file_session.record_comparison_cases(diff_cases)
    stage_timings.append(_stage_finish(
        comparison_timer,
        paper_id=paper_id,
        metadata={
            "filtered_pair_count": sum(len(hits) for hits in comparison_hits.values()),
            "edge_count": len(candidate_graph_edges),
            "case_count": len(diff_cases),
            "workflow": comparison_mode,
        },
    ))

    def adjudication_context(case: BronzeDiffCase) -> AdjudicationContextBundle:
        candidates = [candidates_by_id[candidate_id] for candidate_id in case.candidate_ids if candidate_id in candidates_by_id]
        return AdjudicationContextBundle(
            case=case,
            candidates=candidates,
            source_context=_source_context_for_candidates(
                candidates,
                source_context_by_span_id or {},
                fallback=source_context,
            ),
            candidate_order_seed=case.case_id,
        )

    def adjudicate_cases(cases: list[BronzeDiffCase]) -> list[tuple[AdjudicationConsensus, AdjudicationDecision | None]]:
        contexts = [adjudication_context(case) for case in cases]
        if file_session is not None:
            consensus_records = file_session.run_adjudication(
                contexts,
                passes=adjudication_passes,
                tiebreak_pass=tiebreak_pass,
                direct_judge_confidence=direct_judge_confidence,
                progress_sink=adjudication_progress_sink,
            )
        else:
            consensus_records = run_adjudication_cases(
                contexts,
                passes=adjudication_passes,
                tiebreak_pass=tiebreak_pass,
                direct_judge_confidence=direct_judge_confidence,
                batch_size=max(1, int(adjudication_batch_size or 1)),
                max_workers=max(1, int(adjudication_max_workers or 1)),
                progress_sink=adjudication_progress_sink,
            )
        return [
            (consensus, _decision_from_consensus(context.case, consensus, context.candidates))
            for context, consensus in zip(contexts, consensus_records, strict=True)
        ]

    adjudication_timer = _stage_start("silver_adjudication", "Adjudication")
    worker_count = max(1, min(int(adjudication_max_workers or 1), len(diff_cases) or 1))
    adjudicated = adjudicate_cases(diff_cases)
    consensus_records = [consensus for consensus, _decision in adjudicated]
    _raise_for_operational_adjudication_failures(consensus_records, stage="primary")
    decisions = [decision for _consensus, decision in adjudicated if decision is not None]
    stage_timings.append(_stage_finish(
        adjudication_timer,
        paper_id=paper_id,
        metadata={
            "case_count": len(diff_cases),
            "decision_count": len(decisions),
            "worker_count": worker_count,
            "batch_size": max(1, int(adjudication_batch_size or 1)),
            "workflow": "file_agent" if file_session is not None else "legacy",
        },
    ))

    consolidation_timer = _stage_start("silver_consolidation", "Consolidation")
    if file_session is not None:
        consolidation_cases: list[BronzeDiffCase] = []
        consolidation_edges: list[CandidatePairEdge] = []
        consolidation_pairing = {"workflow": "file_agent_canonicalizer"}
    else:
        consolidation_cases, consolidation_edges, consolidation_pairing = _build_consolidation_cases(
            paper_id=paper_id,
            candidates=candidate_pool,
            decisions=decisions,
            relation_classifier=consolidation_relation_classifier or relation_classifier,
            existing_cases=diff_cases,
        )
        if consolidation_cases:
            candidate_graph_edges = _dedupe_candidate_graph_edges([*candidate_graph_edges, *consolidation_edges])
            diff_cases = _dedupe_cases([*diff_cases, *consolidation_cases])
            worker_count = max(1, min(int(adjudication_max_workers or 1), len(consolidation_cases) or 1))
            adjudicated_consolidation = adjudicate_cases(consolidation_cases)
            consolidation_consensus = [consensus for consensus, _decision in adjudicated_consolidation]
            _raise_for_operational_adjudication_failures(consolidation_consensus, stage="consolidation")
            consensus_records.extend(consolidation_consensus)
            decisions.extend(decision for _consensus, decision in adjudicated_consolidation if decision is not None)
    stage_timings.append(_stage_finish(
        consolidation_timer,
        paper_id=paper_id,
        metadata={
            "case_count": len(consolidation_cases),
            "edge_count": len(consolidation_edges),
            "batch_size": max(1, int(adjudication_batch_size or 1)),
            **consolidation_pairing,
        },
    ))

    canonical_timer = _stage_start("silver_canonicalization", "Silver canonicalization")
    unresolved_candidate_ids = _unresolved_candidate_ids(diff_cases, decisions)
    candidate_ids_for_equivalence = _candidate_ids_retained_for_silver(
        candidate_pool,
        decisions,
        [],
        excluded_candidate_ids=unresolved_candidate_ids,
    )
    cases_by_id = {case.case_id: case for case in diff_cases}
    equivalent_candidate_groups = _dedupe_equivalent_candidate_groups(
        _equivalent_candidate_groups_from_decisions(
            [
                decision
                for decision in decisions
                if any(candidate_id in candidate_ids_for_equivalence for candidate_id in decision.accepted_candidate_ids)
            ],
            cases_by_id=cases_by_id,
        )
    )

    silver_record = build_silver_record(
        paper_id=paper_id,
        silver_record_id=silver_record_id,
        bronze_record_id=bronze_record_id,
        candidates=candidate_pool,
        decisions=decisions,
        equivalent_candidate_groups=equivalent_candidate_groups,
        excluded_candidate_ids=unresolved_candidate_ids,
    )
    if file_session is not None:
        try:
            silver_record = file_session.run_canonicalization(
                baseline_record=silver_record,
                decisions=decisions,
            )
        except Exception as exc:
            if not file_session.fallback_to_legacy:
                raise
            LOGGER.exception("File-agent canonicalization failed for paper=%s; using baseline Silver.", paper_id)
            workflow_fallbacks.append(f"canonicalization:{type(exc).__name__}")
    stage_timings.append(_stage_finish(
        canonical_timer,
        paper_id=paper_id,
        metadata={
            "silver_unit_count": len(silver_record.silver_units),
            "equivalent_group_count": len(equivalent_candidate_groups),
            "unresolved_candidate_count": len(unresolved_candidate_ids),
            "workflow": "file_agent" if file_session is not None else "legacy",
        },
    ))

    importance_timer = _stage_start("silver_importance", "Importance tagging")
    if file_session is None:
        silver_record = apply_silver_importance(
            silver_record,
            classifier=importance_classifier,
            paper_context=paper_context or _paper_context_from_artifact(bronze_artifact),
            source_context=source_context,
        )
    stage_timings.append(_stage_finish(
        importance_timer,
        paper_id=paper_id,
        metadata={
            "enabled": importance_classifier is not None,
            "integrated_into_canonicalization": file_session is not None,
            "workflow": "file_agent_canonicalizer" if file_session is not None else "importance_classifier",
        },
    ))

    graph_timer = _stage_start("silver_graph_metadata", "Graph metadata")
    silver_record.metadata["comparison_graph"] = _comparison_graph_metadata(
        candidate_graph_edges=candidate_graph_edges,
        diff_cases=diff_cases,
        equivalent_candidate_groups=equivalent_candidate_groups,
        adjudication_consensus=consensus_records,
        adjudication_decisions=decisions,
    )
    if file_session is not None:
        try:
            workflow_metadata = file_session.finalize(
                status="complete_with_fallback" if workflow_fallbacks else "complete",
                metadata={"fallbacks": workflow_fallbacks},
            )
            existing_workflow_metadata = silver_record.metadata.get("file_agent_workflow")
            silver_record.metadata["file_agent_workflow"] = {
                **(existing_workflow_metadata if isinstance(existing_workflow_metadata, dict) else {}),
                **workflow_metadata,
                "fallbacks": workflow_fallbacks,
            }
        except Exception:
            LOGGER.exception("Could not finalize file-agent workspace for paper=%s.", paper_id)
    stage_timings.append(_stage_finish(graph_timer, paper_id=paper_id))

    scoring_timer = _stage_start("silver_scoring", "Silver scoring")
    scores = score_silver_jobs_parallel(
        [SilverScoringJob(submission=submission, silver_record=silver_record) for submission in miner_submissions]
    )
    stage_timings.append(_stage_finish(
        scoring_timer,
        paper_id=paper_id,
        metadata={"score_count": len(scores)},
    ))
    return PaperSilverPipelineResult(
        paper_id=paper_id,
        bronze_candidates=bronze_candidates,
        miner_submissions=miner_submissions,
        candidate_graph_edges=candidate_graph_edges,
        diff_cases=diff_cases,
        adjudication_consensus=consensus_records,
        adjudication_decisions=decisions,
        silver_record=silver_record,
        scores=scores,
        stage_timings=stage_timings,
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
    max_workers: int = 8,
) -> BatchScoreResult:
    paper_scores = score_silver_jobs_parallel(jobs, max_workers=max_workers)
    return score_batch(batch_id=batch_id, paper_scores=paper_scores)


def _score_job(job: SilverScoringJob) -> SilverScoreBreakdown:
    return score_miner_against_silver(
        miner_id=job.submission.miner_id,
        miner_candidates=job.submission.candidates,
        silver_record=job.silver_record,
        normal_findings=job.submission.normal_findings,
    )


def _stage_start(key: str, label: str) -> dict:
    return {
        "key": key,
        "label": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "_started_monotonic": time.perf_counter(),
    }


def _stage_finish(timer: dict, *, paper_id: str, metadata: dict | None = None) -> dict:
    ended = datetime.now(timezone.utc)
    started = float(timer.get("_started_monotonic") or time.perf_counter())
    payload = {
        "key": str(timer.get("key") or ""),
        "label": str(timer.get("label") or ""),
        "started_at": str(timer.get("started_at") or ""),
        "ended_at": ended.isoformat(),
        "duration_seconds": round(max(0.0, time.perf_counter() - started), 3),
    }
    stage_metadata = {"paper_id": paper_id}
    if metadata:
        stage_metadata.update(metadata)
    payload["metadata"] = stage_metadata
    return payload


def _select_assessed_candidates(
    candidates: list[ComparisonCandidate],
    assessments: list[dict] | None,
    *,
    max_claims: int,
) -> list[ComparisonCandidate]:
    if assessments is None:
        return candidates[:max_claims]
    assessment_by_claim_id = {
        str(row.get("claim_id") or ""): row
        for row in assessments
        if isinstance(row, dict) and str(row.get("claim_id") or "")
    }
    relevance_order = {"central": 0, "supporting": 1}
    selected: list[tuple[tuple[int, int, str], ComparisonCandidate]] = []
    for candidate in candidates:
        assessment = assessment_by_claim_id.get(candidate.record_id)
        if not assessment:
            continue
        evidence_status = str(assessment.get("evidence_status") or "")
        paper_relevance = str(assessment.get("paper_relevance") or "")
        if evidence_status != "supported" or paper_relevance not in relevance_order:
            continue
        try:
            priority_rank = max(1, int(assessment.get("priority_rank") or 1))
        except (TypeError, ValueError):
            priority_rank = 1
        selected.append(
            (
                (relevance_order[paper_relevance], priority_rank, candidate.record_id),
                candidate.model_copy(
                    update={
                        "metadata": {
                            **candidate.metadata,
                            "diagnostic_claim_assessment": dict(assessment),
                        }
                    }
                ),
            )
        )
    selected.sort(key=lambda item: item[0])
    return [candidate for _key, candidate in selected[:max_claims]]


def _apply_case_budget(
    submissions: list[MinerPaperSubmission],
    *,
    bronze_candidate_count: int,
    max_adjudication_cases: int,
) -> tuple[list[MinerPaperSubmission], int]:
    candidate_budget = max_adjudication_cases - bronze_candidate_count
    if candidate_budget < 0:
        raise RuntimeError(
            "Bronze candidate count exceeds the configured adjudication case ceiling: "
            f"bronze={bronze_candidate_count} ceiling={max_adjudication_cases}."
        )
    retained_by_miner: dict[str, list[ComparisonCandidate]] = {
        submission.miner_id: [] for submission in submissions
    }
    max_depth = max((len(submission.candidates) for submission in submissions), default=0)
    retained_count = 0
    for candidate_index in range(max_depth):
        for submission in submissions:
            if retained_count >= candidate_budget:
                break
            if candidate_index >= len(submission.candidates):
                continue
            retained_by_miner[submission.miner_id].append(
                submission.candidates[candidate_index]
            )
            retained_count += 1
        if retained_count >= candidate_budget:
            break
    original_count = sum(len(submission.candidates) for submission in submissions)
    bounded = [
        MinerPaperSubmission(
            miner_id=submission.miner_id,
            paper_id=submission.paper_id,
            candidates=retained_by_miner[submission.miner_id],
            normal_findings=submission.normal_findings,
        )
        for submission in submissions
    ]
    return bounded, original_count - retained_count


def _comparison_cases_from_graph(
    *,
    paper_id: str,
    bronze_candidates: list[ComparisonCandidate],
    miner_submissions: list[MinerPaperSubmission],
    candidate_graph_edges: list[CandidatePairEdge],
) -> list[BronzeDiffCase]:
    bronze_ids = {candidate.candidate_id for candidate in bronze_candidates}
    miner_candidates = {
        candidate.candidate_id: candidate
        for submission in miner_submissions
        for candidate in submission.candidates
    }
    edges_by_miner_candidate: dict[str, list[CandidatePairEdge]] = {
        candidate_id: [] for candidate_id in miner_candidates
    }
    paired_bronze_ids: set[str] = set()
    for edge in candidate_graph_edges:
        if edge.left_candidate_id in bronze_ids and edge.right_candidate_id in miner_candidates:
            bronze_id = edge.left_candidate_id
            miner_id = edge.right_candidate_id
        elif edge.right_candidate_id in bronze_ids and edge.left_candidate_id in miner_candidates:
            bronze_id = edge.right_candidate_id
            miner_id = edge.left_candidate_id
        else:
            continue
        edges_by_miner_candidate[miner_id].append(edge)
        paired_bronze_ids.add(bronze_id)

    cases: list[BronzeDiffCase] = []
    for candidate_id, candidate in sorted(miner_candidates.items()):
        edges = sorted(
            edges_by_miner_candidate[candidate_id],
            key=lambda edge: (-edge.confidence, edge.edge_id),
        )
        connected_bronze_ids = sorted({
            endpoint
            for edge in edges
            for endpoint in (edge.left_candidate_id, edge.right_candidate_id)
            if endpoint in bronze_ids
        })
        if not edges:
            mismatch_type = "EXTRA_FROM_MINER"
            candidate_ids = [candidate_id]
            question = "Is this miner-only candidate a valid, paper-relevant improvement?"
        else:
            mismatch_type = _combined_mismatch_type(edges)
            candidate_ids = [*connected_bronze_ids, candidate_id]
            question = (
                "How should this miner candidate and all of its candidate Bronze relations "
                "resolve into Silver?"
            )
        cases.append(
            BronzeDiffCase(
                case_id=_graph_case_id(paper_id, mismatch_type, candidate_ids),
                paper_id=paper_id,
                miner_id=candidate.miner_id or "unknown",
                mismatch_type=mismatch_type,  # type: ignore[arg-type]
                candidate_ids=candidate_ids,
                bronze_candidate_id=connected_bronze_ids[0] if connected_bronze_ids else None,
                miner_candidate_id=candidate_id,
                question=question,
                metadata={
                    "candidate_graph_edge": (
                        edges[0].model_dump(mode="json") if edges else None
                    ),
                    "candidate_graph_edges": [edge.model_dump(mode="json") for edge in edges],
                    "comparison_graph": {
                        "edge_ids": [edge.edge_id for edge in edges],
                        "relations": sorted({edge.relation for edge in edges}),
                        "grouped_by_miner_candidate": True,
                    },
                },
            )
        )

    for bronze in bronze_candidates:
        if bronze.candidate_id in paired_bronze_ids:
            continue
        cases.append(
            BronzeDiffCase(
                case_id=_graph_case_id(
                    paper_id,
                    "MISSING_FROM_MINER",
                    [bronze.candidate_id],
                ),
                paper_id=paper_id,
                miner_id="graph",
                mismatch_type="MISSING_FROM_MINER",
                candidate_ids=[bronze.candidate_id],
                bronze_candidate_id=bronze.candidate_id,
                miner_candidate_id=None,
                question="Is this Bronze-only candidate valid and required in Silver?",
                metadata={"comparison_graph": {"global_bronze_only": True}},
            )
        )
    return _dedupe_cases(cases)


def _combined_mismatch_type(edges: list[CandidatePairEdge]) -> str:
    relations = {edge.relation for edge in edges}
    if "contradiction" in relations:
        return "CONTRADICTION"
    if relations & {"partial_overlap", "insufficient_context"}:
        return "SEMANTIC_EQUIVALENCE_UNCERTAIN"
    if "compatible_split_merge" in relations:
        return "SEMANTIC_EQUIVALENCE_UNCERTAIN"
    if "compatible_refinement" in relations:
        return "COMPATIBLE_REFINEMENT"
    return "SEMANTIC_EQUIVALENCE_CANDIDATE"


def _dedupe_cases(cases: Iterable[BronzeDiffCase]) -> list[BronzeDiffCase]:
    by_key: dict[tuple[str, str, tuple[str, ...]], BronzeDiffCase] = {}
    for case in cases:
        key = (case.miner_id, case.mismatch_type, tuple(sorted(case.candidate_ids)))
        if key not in by_key:
            by_key[key] = case
    return list(by_key.values())


def _dedupe_candidate_graph_edges(edges: Iterable[CandidatePairEdge]) -> list[CandidatePairEdge]:
    by_key: dict[tuple[str, str], CandidatePairEdge] = {}
    for edge in edges:
        key = tuple(sorted((edge.left_candidate_id, edge.right_candidate_id)))
        existing = by_key.get(key)
        if existing is None or edge.confidence > existing.confidence:
            by_key[key] = edge
    return sorted(by_key.values(), key=lambda edge: (-edge.confidence, edge.edge_id))


def _build_consolidation_cases(
    *,
    paper_id: str,
    candidates: list[ComparisonCandidate],
    decisions: list[AdjudicationDecision],
    relation_classifier: RelationClassifier | None,
    existing_cases: list[BronzeDiffCase],
) -> tuple[list[BronzeDiffCase], list[CandidatePairEdge], dict[str, int]]:
    retained_ids = {
        candidate_id
        for decision in decisions
        for candidate_id in [*decision.accepted_candidate_ids, *decision.valid_alternative_candidate_ids]
    }
    if len(retained_ids) < 2:
        return [], [], {
            "retained_candidate_count": len(retained_ids),
            "representative_candidate_count": len(retained_ids),
            "unbounded_pair_count": 0,
            "filtered_pair_count": 0,
        }
    retained = [candidate for candidate in candidates if candidate.candidate_id in retained_ids]
    cases_by_id = {case.case_id: case for case in existing_cases}
    existing_equivalence_groups = _dedupe_equivalent_candidate_groups(
        _equivalent_candidate_groups_from_decisions(decisions, cases_by_id=cases_by_id)
    )
    representatives = _consolidation_representatives(retained, existing_equivalence_groups)
    existing_pairs = _candidate_pairs_from_cases(existing_cases)
    unbounded_hits = filter_candidate_pairs(
        representatives,
        representatives,
        include_exact_statement_matches=True,
    )
    bounded_hits = bound_consolidation_pair_hits(
        unbounded_hits,
        excluded_unordered_pairs=existing_pairs,
    )
    candidate_edges = classify_filtered_candidate_pairs(
        bounded_hits,
        relation_classifier=relation_classifier,
    )
    cases: list[BronzeDiffCase] = []
    edges: list[CandidatePairEdge] = []
    seen_pairs: set[tuple[str, str]] = set()
    for edge in candidate_edges:
        if edge.left_candidate_id == edge.right_candidate_id:
            continue
        if not _edge_can_open_consolidation_case(edge):
            continue
        pair_key = tuple(sorted((edge.left_candidate_id, edge.right_candidate_id)))
        if pair_key in seen_pairs or pair_key in existing_pairs:
            continue
        seen_pairs.add(pair_key)
        edge.filter_sources = sorted(set(edge.filter_sources + ["silver_consolidation"]))
        edge.metadata = {**edge.metadata, "consolidation": True}
        edges.append(edge)
        cases.append(
            BronzeDiffCase(
                case_id=_graph_case_id(paper_id, "SILVER_UNIT_CONSOLIDATION", list(pair_key)),
                paper_id=paper_id,
                miner_id=_miner_id_for_case_candidates(edge, candidates),
                mismatch_type=_consolidation_mismatch_type(edge.relation),
                candidate_ids=list(pair_key),
                bronze_candidate_id=next((candidate_id for candidate_id in pair_key if candidate_id.startswith("bronze:")), None),
                miner_candidate_id=next((candidate_id for candidate_id in pair_key if candidate_id.startswith("miner:")), None),
                question="Do these accepted candidates represent the same final Silver claim unit?",
                metadata={
                    "candidate_graph_edge": edge.model_dump(mode="json"),
                    "comparison_graph": {
                        "edge_id": edge.edge_id,
                        "relation": edge.relation,
                        "confidence": edge.confidence,
                        "filter_sources": edge.filter_sources,
                        "consolidation": True,
                    },
                },
            )
        )
    return cases, edges, {
        "retained_candidate_count": len(retained),
        "representative_candidate_count": len(representatives),
        "unbounded_pair_count": len({
            tuple(sorted((hit.left.candidate_id, hit.right.candidate_id)))
            for hit in unbounded_hits
            if hit.left.candidate_id != hit.right.candidate_id
        }),
        "filtered_pair_count": len(bounded_hits),
    }


def _consolidation_representatives(
    retained: list[ComparisonCandidate],
    equivalence_groups: list[list[str]],
) -> list[ComparisonCandidate]:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in retained}
    replaced_ids: set[str] = set()
    for group in equivalence_groups:
        group_candidates = [
            candidates_by_id[candidate_id]
            for candidate_id in group
            if candidate_id in candidates_by_id
        ]
        if len(group_candidates) < 2:
            continue
        representative = sorted(
            group_candidates,
            key=lambda candidate: (
                -len(candidate.source_span_ids),
                -len(candidate.evidence_ids),
                -len(candidate.source_quotes),
                -len(candidate.statement),
                candidate.candidate_id,
            ),
        )[0]
        replaced_ids.update(
            candidate.candidate_id
            for candidate in group_candidates
            if candidate.candidate_id != representative.candidate_id
        )
    return [candidate for candidate in retained if candidate.candidate_id not in replaced_ids]


def _edge_can_open_consolidation_case(edge: CandidatePairEdge) -> bool:
    if edge.relation == "semantic_equivalent":
        return edge.confidence >= CONSOLIDATION_SEMANTIC_CONFIDENCE
    if edge.relation in {"compatible_refinement", "compatible_split_merge"}:
        return edge.confidence >= CONSOLIDATION_COMPATIBLE_CONFIDENCE
    return False


def _candidate_pairs_from_cases(cases: list[BronzeDiffCase]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for case in cases:
        if len(case.candidate_ids) < 2:
            continue
        ids = sorted(candidate_id for candidate_id in case.candidate_ids if candidate_id)
        for index, left in enumerate(ids):
            for right in ids[index + 1 :]:
                pairs.add((left, right))
    return pairs


def _consolidation_mismatch_type(relation: str):
    if relation == "semantic_equivalent":
        return "SEMANTIC_EQUIVALENCE_CANDIDATE"
    if relation == "compatible_refinement":
        return "COMPATIBLE_REFINEMENT"
    if relation == "contradiction":
        return "CONTRADICTION"
    return "SEMANTIC_EQUIVALENCE_UNCERTAIN"


def _miner_id_for_case_candidates(edge: CandidatePairEdge, candidates: list[ComparisonCandidate]) -> str:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    miner_ids = sorted(
        {
            candidate.miner_id
            for candidate_id in [edge.left_candidate_id, edge.right_candidate_id]
            if (candidate := candidates_by_id.get(candidate_id)) and candidate.miner_id
        }
    )
    return miner_ids[0] if len(miner_ids) == 1 else "graph"


def _graph_case_id(paper_id: str, mismatch_type: str, candidate_ids: list[str]) -> str:
    digest = hashlib.sha256("|".join([paper_id, mismatch_type, *sorted(candidate_ids)]).encode("utf-8")).hexdigest()[:16]
    return f"candidate_graph_{digest}"


def _dedupe_equivalent_candidate_groups(groups: Iterable[list[str]]) -> list[list[str]]:
    parent: dict[str, str] = {}
    bronze_ids_by_root: dict[str, set[str]] = {}

    def find(item: str) -> str:
        if item not in parent:
            parent[item] = item
            bronze_ids_by_root[item] = {item} if item.startswith("bronze:") else set()
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        left_bronze_ids = bronze_ids_by_root.get(left_root, set())
        right_bronze_ids = bronze_ids_by_root.get(right_root, set())
        # A noisy miner candidate may be judged equivalent to more than one
        # Bronze claim. It must not become a transitive bridge that collapses
        # distinct reference anchors into one scored unit.
        if left_bronze_ids and right_bronze_ids and left_bronze_ids != right_bronze_ids:
            return
        parent[right_root] = left_root
        bronze_ids_by_root[left_root] = left_bronze_ids | right_bronze_ids
        bronze_ids_by_root.pop(right_root, None)

    for group in groups:
        ids = [candidate_id for candidate_id in group if candidate_id]
        if len(ids) < 2:
            continue
        first = ids[0]
        for candidate_id in ids[1:]:
            union(first, candidate_id)
    components: dict[str, list[str]] = {}
    for candidate_id in sorted(parent):
        components.setdefault(find(candidate_id), []).append(candidate_id)
    return [sorted(ids) for ids in components.values() if len(ids) >= 2]


def _equivalent_candidate_groups_from_decisions(
    decisions: Iterable[AdjudicationDecision],
    *,
    cases_by_id: dict[str, BronzeDiffCase] | None = None,
) -> list[list[str]]:
    groups: list[list[str]] = []
    ordered_decisions = sorted(
        decisions,
        key=lambda decision: (-_decision_equivalence_confidence(decision), decision.case_id),
    )
    for decision in ordered_decisions:
        if not _decision_creates_equivalence_group(decision, cases_by_id or {}):
            continue
        candidate_ids = [*decision.accepted_candidate_ids, *decision.valid_alternative_candidate_ids]
        if len(candidate_ids) >= 2:
            groups.append(candidate_ids)
    return groups


def _decision_equivalence_confidence(decision: AdjudicationDecision) -> float:
    if decision.consensus is None:
        return 0.0
    return float(decision.consensus.final_confidence or 0.0)


def _decision_creates_equivalence_group(
    decision: AdjudicationDecision,
    cases_by_id: dict[str, BronzeDiffCase],
) -> bool:
    if not decision.same_silver_unit:
        return False
    if decision.disposition not in {"accepted_improvement", "benign_difference", "both_valid"}:
        return False
    case = cases_by_id.get(decision.case_id)
    if case is None:
        return decision.disposition == "benign_difference"
    edge = case.metadata.get("candidate_graph_edge") if isinstance(case.metadata, dict) else None
    relation = edge.get("relation") if isinstance(edge, dict) else None
    if relation in SAME_UNIT_RELATIONS:
        return True
    return decision.disposition == "benign_difference"


def _comparison_graph_metadata(
    *,
    candidate_graph_edges: list[CandidatePairEdge],
    diff_cases: list[BronzeDiffCase],
    equivalent_candidate_groups: list[list[str]],
    adjudication_consensus: list[AdjudicationConsensus],
    adjudication_decisions: list[AdjudicationDecision],
) -> dict:
    case_id_by_pair = {
        tuple(sorted(case.candidate_ids)): case.case_id
        for case in diff_cases
        if len(case.candidate_ids) >= 2
    }
    consensus_by_case_id = {item.case_id: item for item in adjudication_consensus}
    decision_by_case_id = {item.case_id: item for item in adjudication_decisions}
    return {
        "version": "candidate_graph_v1",
        "edge_count": len(candidate_graph_edges),
        "case_count": len(diff_cases),
        "equivalence_group_count": len(equivalent_candidate_groups),
        "equivalent_candidate_groups": equivalent_candidate_groups,
        "cases": [
            _comparison_graph_case_metadata(
                case=case,
                consensus_by_case_id=consensus_by_case_id,
                decision_by_case_id=decision_by_case_id,
            )
            for case in diff_cases
        ],
        "edges": [
            _comparison_graph_edge_metadata(
                edge=edge,
                case_id=case_id_by_pair.get(tuple(sorted([edge.left_candidate_id, edge.right_candidate_id]))),
                consensus_by_case_id=consensus_by_case_id,
                decision_by_case_id=decision_by_case_id,
            )
            for edge in candidate_graph_edges
        ],
    }


def _comparison_graph_case_metadata(
    *,
    case: BronzeDiffCase,
    consensus_by_case_id: dict[str, AdjudicationConsensus],
    decision_by_case_id: dict[str, AdjudicationDecision],
) -> dict:
    payload = {
        "case_id": case.case_id,
        "paper_id": case.paper_id,
        "miner_id": case.miner_id,
        "mismatch_type": case.mismatch_type,
        "candidate_ids": case.candidate_ids,
        "bronze_candidate_id": case.bronze_candidate_id,
        "miner_candidate_id": case.miner_candidate_id,
        "question": case.question,
    }
    edge = case.metadata.get("candidate_graph_edge") if isinstance(case.metadata, dict) else None
    if isinstance(edge, dict):
        payload["candidate_graph_edge"] = edge
        payload["edge_id"] = edge.get("edge_id")
        payload["relation"] = edge.get("relation")
        payload["relation_confidence"] = edge.get("confidence")
        payload["filter_sources"] = edge.get("filter_sources") if isinstance(edge.get("filter_sources"), list) else []
    consensus = consensus_by_case_id.get(case.case_id)
    if consensus is not None:
        payload.update(
            {
                "route": consensus.route,
                "final_disposition": consensus.final_disposition,
                "final_confidence": consensus.final_confidence,
                "agreement_rate": consensus.agreement_rate,
                "vote_count": len(consensus.votes),
            }
        )
    decision = decision_by_case_id.get(case.case_id)
    if decision is not None:
        payload.update(
            {
                "decision_disposition": decision.disposition,
                "accepted_candidate_ids": decision.accepted_candidate_ids,
                "rejected_candidate_ids": decision.rejected_candidate_ids,
                "valid_alternative_candidate_ids": decision.valid_alternative_candidate_ids,
                "same_silver_unit": decision.same_silver_unit,
                "creates_required_silver_unit": decision.creates_required_silver_unit,
                "creates_optional_improvement_unit": decision.creates_optional_improvement_unit,
                "importance": decision.importance,
            }
        )
    return payload


def _comparison_graph_edge_metadata(
    *,
    edge: CandidatePairEdge,
    case_id: str | None,
    consensus_by_case_id: dict[str, AdjudicationConsensus],
    decision_by_case_id: dict[str, AdjudicationDecision],
) -> dict:
    payload = {**edge.model_dump(mode="json"), "case_id": case_id}
    if not case_id:
        return payload
    consensus = consensus_by_case_id.get(case_id)
    if consensus is not None:
        payload.update(
            {
                "route": consensus.route,
                "final_disposition": consensus.final_disposition,
                "final_confidence": consensus.final_confidence,
                "agreement_rate": consensus.agreement_rate,
                "vote_count": len(consensus.votes),
            }
        )
    decision = decision_by_case_id.get(case_id)
    if decision is not None:
        payload.update(
            {
                "decision_disposition": decision.disposition,
                "accepted_candidate_ids": decision.accepted_candidate_ids,
                "rejected_candidate_ids": decision.rejected_candidate_ids,
                "valid_alternative_candidate_ids": decision.valid_alternative_candidate_ids,
                "same_silver_unit": decision.same_silver_unit,
                "creates_required_silver_unit": decision.creates_required_silver_unit,
                "creates_optional_improvement_unit": decision.creates_optional_improvement_unit,
                "importance": decision.importance,
            }
        )
    return payload


def _candidate_ids_retained_for_silver(
    candidates: list[ComparisonCandidate],
    decisions: list[AdjudicationDecision],
    comparison_equivalent_candidate_groups: list[list[str]],
    *,
    excluded_candidate_ids: set[str] | None = None,
) -> set[str]:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    excluded_ids = set(excluded_candidate_ids or set())
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
        and candidate.candidate_id not in excluded_ids
        and (candidate.candidate_id not in rejected_bronze_ids or candidate.candidate_id in accepted_bronze_ids)
    }
    for decision in decisions:
        retained.update(candidate_id for candidate_id in decision.accepted_candidate_ids if candidate_id in candidates_by_id)
        retained.update(candidate_id for candidate_id in decision.valid_alternative_candidate_ids if candidate_id in candidates_by_id)
    for group in comparison_equivalent_candidate_groups:
        retained.update(candidate_id for candidate_id in group if candidate_id in candidates_by_id and candidate_id not in excluded_ids)
    return retained


def _unresolved_candidate_ids(cases: list[BronzeDiffCase], decisions: list[AdjudicationDecision]) -> set[str]:
    resolved_case_ids = {decision.case_id for decision in decisions}
    accepted_candidate_ids = {
        candidate_id
        for decision in decisions
        for candidate_id in [*decision.accepted_candidate_ids, *decision.valid_alternative_candidate_ids]
    }
    unresolved = {
        candidate_id
        for case in cases
        if case.case_id not in resolved_case_ids
        for candidate_id in case.candidate_ids
        if candidate_id
    }
    return unresolved - accepted_candidate_ids


def _raise_for_operational_adjudication_failures(
    consensus_records: list[AdjudicationConsensus],
    *,
    stage: str,
) -> None:
    failed_case_ids = [
        consensus.case_id
        for consensus in consensus_records
        if consensus.final_disposition is None
        and any(_is_operational_adjudication_failure(vote) for vote in consensus.votes)
    ]
    if not failed_case_ids:
        return
    raise RuntimeError(
        f"Silver {stage} adjudication failed operationally for "
        f"{len(failed_case_ids)}/{len(consensus_records)} cases; refusing to score an incomplete Silver record."
    )


def _is_operational_adjudication_failure(vote: object) -> bool:
    findings = getattr(vote, "material_findings", [])
    return any(
        finding in {"adjudication_batch_failed", "adjudication_pass_failed"}
        for finding in findings
    )


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
        creates_required = bool(bronze_ids) and case.mismatch_type != "EXTRA_FROM_MINER"
        creates_optional = not bool(bronze_ids) or case.mismatch_type == "EXTRA_FROM_MINER"
        return AdjudicationDecision(
            case_id=case.case_id,
            disposition=disposition,
            accepted_candidate_ids=accepted,
            same_silver_unit=_case_decision_same_silver_unit(case, disposition),
            creates_required_silver_unit=creates_required,
            creates_optional_improvement_unit=creates_optional,
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
    return "supporting"


def _case_decision_same_silver_unit(case: BronzeDiffCase, disposition: str) -> bool:
    if len(case.candidate_ids) < 2:
        return True
    edge = case.metadata.get("candidate_graph_edge") if isinstance(case.metadata, dict) else None
    relation = edge.get("relation") if isinstance(edge, dict) else None
    if relation == "semantic_equivalent" and disposition in {"accepted_improvement", "benign_difference", "both_valid"}:
        return True
    if relation in {"compatible_refinement", "compatible_split_merge"} and disposition in {"accepted_improvement", "benign_difference"}:
        return True
    return disposition == "benign_difference"


def _paper_context_from_artifact(artifact: dict) -> dict:
    paper = artifact.get("paper") if isinstance(artifact.get("paper"), dict) else {}
    logic = artifact.get("logic") if isinstance(artifact.get("logic"), dict) else {}
    return {
        "paper_id": paper.get("paper_id") or "",
        "title": paper.get("title") or paper.get("paper_title") or "",
        "abstract": paper.get("abstract") or paper.get("summary") or "",
        "claims_summary": logic.get("claims_summary") or "",
    }
