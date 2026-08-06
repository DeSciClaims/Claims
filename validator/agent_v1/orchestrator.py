from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationDecision
from .adjudication_runner import AdjudicationPass, run_adjudication_case
from .batch_scoring import BatchScoreResult, score_batch
from .bronze_diff import compare_miner_to_bronze_result
from .comparison_models import BronzeDiffCase, CandidatePairEdge, ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from .pairing import RelationClassifier, build_candidate_pairs
from .record_projection import project_agent_artifact
from .relation_classifier import classify_candidate_pair
from .silver_builder import build_silver_record
from .silver_scoring import score_miner_against_silver


CONSOLIDATION_SEMANTIC_CONFIDENCE = 0.88


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
    candidate_graph_edges: list[CandidatePairEdge]
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
    candidate_graph_edges = _dedupe_candidate_graph_edges(
        edge
        for result in comparison_results
        for edge in result.candidate_graph_edges
    )
    diff_cases = _dedupe_cases(
        case
        for result in comparison_results
        for case in result.cases
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
    consolidation_cases, consolidation_edges = _build_consolidation_cases(
        paper_id=paper_id,
        candidates=candidate_pool,
        decisions=decisions,
        relation_classifier=relation_classifier,
        existing_cases=diff_cases,
    )
    if consolidation_cases:
        candidate_graph_edges = _dedupe_candidate_graph_edges([*candidate_graph_edges, *consolidation_edges])
        diff_cases = _dedupe_cases([*diff_cases, *consolidation_cases])
        worker_count = max(1, min(int(adjudication_max_workers or 1), len(consolidation_cases) or 1))
        if worker_count == 1:
            adjudicated_consolidation = [adjudicate(case) for case in consolidation_cases]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                adjudicated_consolidation = list(executor.map(adjudicate, consolidation_cases))
        consensus_records.extend(consensus for consensus, _decision in adjudicated_consolidation)
        decisions.extend(decision for _consensus, decision in adjudicated_consolidation if decision is not None)
    candidate_ids_for_equivalence = _candidate_ids_retained_for_silver(
        candidate_pool,
        decisions,
        [],
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
    )
    silver_record.metadata["comparison_graph"] = _comparison_graph_metadata(
        candidate_graph_edges=candidate_graph_edges,
        diff_cases=diff_cases,
        equivalent_candidate_groups=equivalent_candidate_groups,
        adjudication_consensus=consensus_records,
        adjudication_decisions=decisions,
    )
    scores = score_silver_jobs_parallel(
        [SilverScoringJob(submission=submission, silver_record=silver_record) for submission in miner_submissions]
    )
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
) -> tuple[list[BronzeDiffCase], list[CandidatePairEdge]]:
    retained_ids = {
        candidate_id
        for decision in decisions
        for candidate_id in [*decision.accepted_candidate_ids, *decision.valid_alternative_candidate_ids]
    }
    if len(retained_ids) < 2:
        return [], []
    retained = [candidate for candidate in candidates if candidate.candidate_id in retained_ids]
    existing_pairs = _candidate_pairs_from_cases(existing_cases)
    candidate_edges = build_candidate_pairs(retained, retained, relation_classifier=relation_classifier)
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
    return cases, edges


def _edge_can_open_consolidation_case(edge: CandidatePairEdge) -> bool:
    return edge.relation == "semantic_equivalent" and edge.confidence >= CONSOLIDATION_SEMANTIC_CONFIDENCE


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
    seen: set[tuple[str, ...]] = set()
    deduped: list[list[str]] = []
    for group in groups:
        key = tuple(sorted(candidate_id for candidate_id in group if candidate_id))
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        deduped.append(list(key))
    return deduped


def _equivalent_candidate_groups_from_decisions(
    decisions: Iterable[AdjudicationDecision],
    *,
    cases_by_id: dict[str, BronzeDiffCase] | None = None,
) -> list[list[str]]:
    groups: list[list[str]] = []
    for decision in decisions:
        if not _decision_creates_equivalence_group(decision, cases_by_id or {}):
            continue
        candidate_ids = [*decision.accepted_candidate_ids, *decision.valid_alternative_candidate_ids]
        if len(candidate_ids) >= 2:
            groups.append(candidate_ids)
    return groups


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
    if relation == "semantic_equivalent":
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
    if any(candidate.importance == "central" for candidate in candidates):
        return "central"
    if any(candidate.importance == "supporting" for candidate in candidates):
        return "supporting"
    return "minor"


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
