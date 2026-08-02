from __future__ import annotations

import hashlib

from .adjudication_models import AdjudicationDecision
from .comparison_models import ComparisonCandidate, InvalidMinerCandidate, ReferenceError, SilverRecord, SilverUnit


def build_silver_record(
    *,
    paper_id: str,
    silver_record_id: str,
    candidates: list[ComparisonCandidate],
    decisions: list[AdjudicationDecision],
    bronze_record_id: str | None = None,
) -> SilverRecord:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    silver_units: list[SilverUnit] = []
    invalid_candidates: list[InvalidMinerCandidate] = []
    reference_errors: list[ReferenceError] = []

    for decision in decisions:
        accepted = [candidates_by_id[candidate_id] for candidate_id in decision.accepted_candidate_ids if candidate_id in candidates_by_id]
        rejected = [candidates_by_id[candidate_id] for candidate_id in decision.rejected_candidate_ids if candidate_id in candidates_by_id]

        for candidate in rejected:
            if candidate.origin == "bronze":
                reference_errors.append(
                    ReferenceError(candidate_id=candidate.candidate_id, reason=decision.rationale, adjudication_case_id=decision.case_id)
                )
            elif candidate.miner_id:
                invalid_candidates.append(
                    InvalidMinerCandidate(
                        candidate_id=candidate.candidate_id,
                        miner_id=candidate.miner_id,
                        reason=decision.rationale,
                        adjudication_case_id=decision.case_id,
                    )
                )

        if not accepted:
            continue

        primary = _primary_candidate(accepted)
        unit_id = decision.silver_unit_id or _silver_unit_id(paper_id, decision.case_id, primary.statement)
        equivalent_candidate_ids = [candidate.candidate_id for candidate in accepted] + decision.valid_alternative_candidate_ids
        silver_units.append(
            SilverUnit(
                silver_unit_id=unit_id,
                paper_id=paper_id,
                statement=primary.statement,
                importance=decision.importance or primary.importance,
                required_for_completeness=decision.creates_required_silver_unit,
                equivalent_candidate_ids=equivalent_candidate_ids,
                evidence_ids=_candidate_evidence_ids(equivalent_candidate_ids, candidates_by_id),
                source_span_ids=primary.source_span_ids,
                adjudication_case_ids=[decision.case_id],
                scoring_mode="accepted_improvement" if decision.creates_optional_improvement_unit else "required",
            )
        )

    return SilverRecord(
        silver_record_id=silver_record_id,
        paper_id=paper_id,
        bronze_record_id=bronze_record_id,
        silver_units=_dedupe_units(silver_units),
        invalid_miner_candidates=invalid_candidates,
        reference_errors=reference_errors,
    )


def _primary_candidate(candidates: list[ComparisonCandidate]) -> ComparisonCandidate:
    miner_candidates = [candidate for candidate in candidates if candidate.origin == "miner"]
    return miner_candidates[0] if miner_candidates else candidates[0]


def _silver_unit_id(paper_id: str, case_id: str, statement: str) -> str:
    digest = hashlib.sha256("|".join([paper_id, case_id, statement]).encode("utf-8")).hexdigest()[:12]
    return f"silver_{digest}"


def _candidate_evidence_ids(candidate_ids: list[str], candidates_by_id: dict[str, ComparisonCandidate]) -> list[str]:
    evidence_ids: list[str] = []
    for candidate_id in candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if candidate:
            evidence_ids.extend(candidate.evidence_ids)
    return sorted(set(evidence_ids))


def _dedupe_units(units: list[SilverUnit]) -> list[SilverUnit]:
    by_id: dict[str, SilverUnit] = {}
    for unit in units:
        if unit.silver_unit_id not in by_id:
            by_id[unit.silver_unit_id] = unit
            continue
        existing = by_id[unit.silver_unit_id]
        existing.equivalent_candidate_ids = sorted(set(existing.equivalent_candidate_ids + unit.equivalent_candidate_ids))
        existing.evidence_ids = sorted(set(existing.evidence_ids + unit.evidence_ids))
        existing.source_span_ids = sorted(set(existing.source_span_ids + unit.source_span_ids))
        existing.adjudication_case_ids = sorted(set(existing.adjudication_case_ids + unit.adjudication_case_ids))
    return list(by_id.values())
