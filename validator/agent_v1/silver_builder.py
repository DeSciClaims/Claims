from __future__ import annotations

import hashlib
import re

from .adjudication_models import AdjudicationDecision
from .comparison_models import ComparisonCandidate, InvalidMinerCandidate, ReferenceError, SilverRecord, SilverUnit


def build_silver_record(
    *,
    paper_id: str,
    silver_record_id: str,
    candidates: list[ComparisonCandidate],
    decisions: list[AdjudicationDecision],
    bronze_record_id: str | None = None,
    equivalent_candidate_groups: list[list[str]] | None = None,
) -> SilverRecord:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    units_by_key: dict[tuple[str, str], SilverUnit] = {}
    invalid_candidates: list[InvalidMinerCandidate] = []
    reference_errors: list[ReferenceError] = []
    seen_invalid_candidates: set[tuple[str, str | None]] = set()
    seen_reference_errors: set[str] = set()

    accepted_bronze_ids: set[str] = set()
    rejected_bronze_ids: set[str] = set()
    for decision in decisions:
        for candidate_id in decision.accepted_candidate_ids:
            candidate = candidates_by_id.get(candidate_id)
            if candidate and candidate.origin == "bronze":
                accepted_bronze_ids.add(candidate_id)
        for candidate_id in decision.rejected_candidate_ids:
            candidate = candidates_by_id.get(candidate_id)
            if candidate and candidate.origin == "bronze":
                rejected_bronze_ids.add(candidate_id)
    filtered_equivalent_candidate_groups = _filter_equivalence_groups(
        equivalent_candidate_groups or [],
        excluded_candidate_ids=rejected_bronze_ids - accepted_bronze_ids,
    )
    equivalence_group_by_candidate_id = _equivalence_group_by_candidate_id(filtered_equivalent_candidate_groups)

    for candidate in candidates:
        if candidate.origin != "bronze":
            continue
        if candidate.candidate_id in rejected_bronze_ids and candidate.candidate_id not in accepted_bronze_ids:
            continue
        _add_or_merge_unit(
            units_by_key,
            paper_id=paper_id,
            accepted=[candidate],
            candidates_by_id=candidates_by_id,
            case_id=None,
            explicit_unit_id=None,
            required_for_completeness=True,
            scoring_mode="required",
            importance=candidate.importance,
            equivalence_group_by_candidate_id=equivalence_group_by_candidate_id,
        )

    for group in filtered_equivalent_candidate_groups:
        accepted = [candidates_by_id[candidate_id] for candidate_id in group if candidate_id in candidates_by_id]
        if len(accepted) < 2 or not any(candidate.origin == "bronze" for candidate in accepted):
            continue
        _add_or_merge_unit(
            units_by_key,
            paper_id=paper_id,
            accepted=accepted,
            candidates_by_id=candidates_by_id,
            case_id=None,
            explicit_unit_id=None,
            required_for_completeness=True,
            scoring_mode="required",
            importance=_primary_candidate(accepted).importance,
            equivalence_group_by_candidate_id=equivalence_group_by_candidate_id,
        )

    for decision in decisions:
        accepted = [candidates_by_id[candidate_id] for candidate_id in decision.accepted_candidate_ids if candidate_id in candidates_by_id]
        rejected = [candidates_by_id[candidate_id] for candidate_id in decision.rejected_candidate_ids if candidate_id in candidates_by_id]

        for candidate in rejected:
            if candidate.origin == "bronze":
                if candidate.candidate_id not in seen_reference_errors:
                    reference_errors.append(
                        ReferenceError(
                            candidate_id=candidate.candidate_id,
                            reason=decision.rationale,
                            adjudication_case_id=decision.case_id,
                        )
                    )
                    seen_reference_errors.add(candidate.candidate_id)
            elif candidate.miner_id:
                invalid_key = (candidate.candidate_id, candidate.miner_id)
                if invalid_key not in seen_invalid_candidates:
                    invalid_candidates.append(
                        InvalidMinerCandidate(
                            candidate_id=candidate.candidate_id,
                            miner_id=candidate.miner_id,
                            reason=decision.rationale,
                            adjudication_case_id=decision.case_id,
                        )
                    )
                    seen_invalid_candidates.add(invalid_key)

        if not accepted:
            continue

        equivalent_candidate_ids = [candidate.candidate_id for candidate in accepted] + decision.valid_alternative_candidate_ids
        unit_candidates = [candidate for candidate_id in equivalent_candidate_ids if (candidate := candidates_by_id.get(candidate_id))]
        if decision.same_silver_unit or len(unit_candidates) <= 1:
            _add_or_merge_unit(
                units_by_key,
                paper_id=paper_id,
                accepted=unit_candidates,
                candidates_by_id=candidates_by_id,
                case_id=decision.case_id,
                explicit_unit_id=decision.silver_unit_id,
                required_for_completeness=decision.creates_required_silver_unit,
                scoring_mode="accepted_improvement" if decision.creates_optional_improvement_unit else "required",
                importance=decision.importance or _primary_candidate(accepted).importance,
                equivalence_group_by_candidate_id=equivalence_group_by_candidate_id,
            )
            continue

        for candidate in unit_candidates:
            required_for_completeness, scoring_mode = _separate_unit_scoring(candidate, decision)
            _add_or_merge_unit(
                units_by_key,
                paper_id=paper_id,
                accepted=[candidate],
                candidates_by_id=candidates_by_id,
                case_id=decision.case_id,
                explicit_unit_id=None,
                required_for_completeness=required_for_completeness,
                scoring_mode=scoring_mode,
                importance=decision.importance or candidate.importance,
                equivalence_group_by_candidate_id=equivalence_group_by_candidate_id,
            )

    return SilverRecord(
        silver_record_id=silver_record_id,
        paper_id=paper_id,
        bronze_record_id=bronze_record_id,
        silver_units=list(units_by_key.values()),
        invalid_miner_candidates=invalid_candidates,
        reference_errors=reference_errors,
    )


def _primary_candidate(candidates: list[ComparisonCandidate]) -> ComparisonCandidate:
    miner_candidates = [candidate for candidate in candidates if candidate.origin == "miner"]
    return miner_candidates[0] if miner_candidates else candidates[0]


def _silver_unit_id(paper_id: str, case_id: str, statement: str) -> str:
    digest = hashlib.sha256("|".join([paper_id, case_id, statement]).encode("utf-8")).hexdigest()[:12]
    return f"silver_{digest}"


def _add_or_merge_unit(
    units_by_key: dict[tuple[str, str], SilverUnit],
    *,
    paper_id: str,
    accepted: list[ComparisonCandidate],
    candidates_by_id: dict[str, ComparisonCandidate],
    case_id: str | None,
    explicit_unit_id: str | None,
    required_for_completeness: bool,
    scoring_mode: str,
    importance: str,
    equivalence_group_by_candidate_id: dict[str, tuple[str, ...]],
) -> None:
    if not accepted:
        return
    primary = _primary_candidate(accepted)
    key = _unit_key(accepted, scoring_mode=scoring_mode, equivalence_group_by_candidate_id=equivalence_group_by_candidate_id)
    candidate_ids = sorted(set(_expanded_candidate_ids(accepted, equivalence_group_by_candidate_id)))
    unit = units_by_key.get(key)
    if unit is None:
        unit = SilverUnit(
            silver_unit_id=explicit_unit_id or _silver_unit_id(paper_id, "|".join(key), primary.statement),
            paper_id=paper_id,
            statement=primary.statement,
            importance=importance,  # type: ignore[arg-type]
            required_for_completeness=required_for_completeness,
            equivalent_candidate_ids=sorted(set(candidate_ids)),
            evidence_ids=_candidate_evidence_ids(candidate_ids, candidates_by_id),
            source_span_ids=_candidate_source_span_ids(candidate_ids, candidates_by_id),
            adjudication_case_ids=[case_id] if case_id else [],
            scoring_mode=scoring_mode,  # type: ignore[arg-type]
        )
        units_by_key[key] = unit
        return
    unit.required_for_completeness = unit.required_for_completeness or required_for_completeness
    unit.equivalent_candidate_ids = sorted(set(unit.equivalent_candidate_ids + candidate_ids))
    unit.evidence_ids = sorted(set(unit.evidence_ids + _candidate_evidence_ids(candidate_ids, candidates_by_id)))
    unit.source_span_ids = sorted(set(unit.source_span_ids + _candidate_source_span_ids(candidate_ids, candidates_by_id)))
    if case_id:
        unit.adjudication_case_ids = sorted(set(unit.adjudication_case_ids + [case_id]))


def _unit_key(
    candidates: list[ComparisonCandidate],
    *,
    scoring_mode: str,
    equivalence_group_by_candidate_id: dict[str, tuple[str, ...]],
) -> tuple[str, str]:
    equivalence_ids = sorted(
        {
            candidate_id
            for candidate in candidates
            for candidate_id in equivalence_group_by_candidate_id.get(candidate.candidate_id, ())
        }
    )
    bronze_ids = sorted(candidate.candidate_id for candidate in candidates if candidate.origin == "bronze")
    group_bronze_ids = sorted(candidate_id for candidate_id in equivalence_ids if candidate_id.startswith("bronze:"))
    if scoring_mode == "required" and bronze_ids:
        if group_bronze_ids:
            return ("required:equivalence", "|".join(equivalence_ids))
        return ("required:bronze", "|".join(bronze_ids))
    miner_ids = sorted(candidate.candidate_id for candidate in candidates if candidate.origin == "miner")
    group_miner_ids = sorted(candidate_id for candidate_id in equivalence_ids if candidate_id.startswith("miner:"))
    if scoring_mode == "accepted_improvement" and miner_ids:
        if group_miner_ids:
            return ("optional:equivalence", "|".join(equivalence_ids))
        return ("optional:miner", "|".join(miner_ids))
    primary = _primary_candidate(candidates)
    return (scoring_mode, _normalize_statement(primary.statement))


def _normalize_statement(value: str) -> str:
    normalized = value.casefold()
    normalized = re.sub(r"[^a-z0-9\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _candidate_evidence_ids(candidate_ids: list[str], candidates_by_id: dict[str, ComparisonCandidate]) -> list[str]:
    evidence_ids: list[str] = []
    for candidate_id in candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if candidate:
            evidence_ids.extend(candidate.evidence_ids)
    return sorted(set(evidence_ids))


def _candidate_source_span_ids(candidate_ids: list[str], candidates_by_id: dict[str, ComparisonCandidate]) -> list[str]:
    source_span_ids: list[str] = []
    for candidate_id in candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if candidate:
            source_span_ids.extend(candidate.source_span_ids)
    return sorted(set(source_span_ids))


def _separate_unit_scoring(candidate: ComparisonCandidate, decision: AdjudicationDecision) -> tuple[bool, str]:
    if candidate.origin == "miner":
        if decision.disposition == "reference_error" and decision.creates_required_silver_unit:
            return True, "required"
        return False, "accepted_improvement"
    return decision.creates_required_silver_unit, "required"


def _expanded_candidate_ids(
    candidates: list[ComparisonCandidate],
    equivalence_group_by_candidate_id: dict[str, tuple[str, ...]],
) -> list[str]:
    candidate_ids: list[str] = []
    for candidate in candidates:
        candidate_ids.extend(equivalence_group_by_candidate_id.get(candidate.candidate_id, (candidate.candidate_id,)))
    return candidate_ids


def _equivalence_group_by_candidate_id(groups: list[list[str]]) -> dict[str, tuple[str, ...]]:
    parent: dict[str, str] = {}

    def find(candidate_id: str) -> str:
        parent.setdefault(candidate_id, candidate_id)
        if parent[candidate_id] != candidate_id:
            parent[candidate_id] = find(parent[candidate_id])
        return parent[candidate_id]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for group in groups:
        candidate_ids = [candidate_id for candidate_id in group if candidate_id]
        if len(candidate_ids) < 2:
            continue
        first = candidate_ids[0]
        for candidate_id in candidate_ids[1:]:
            union(first, candidate_id)

    grouped: dict[str, set[str]] = {}
    for candidate_id in list(parent):
        grouped.setdefault(find(candidate_id), set()).add(candidate_id)

    result: dict[str, tuple[str, ...]] = {}
    for candidate_ids in grouped.values():
        if len(candidate_ids) < 2:
            continue
        group = tuple(sorted(candidate_ids))
        for candidate_id in group:
            result[candidate_id] = group
    return result


def _filter_equivalence_groups(groups: list[list[str]], *, excluded_candidate_ids: set[str]) -> list[list[str]]:
    if not excluded_candidate_ids:
        return groups
    filtered: list[list[str]] = []
    for group in groups:
        candidate_ids = [candidate_id for candidate_id in group if candidate_id not in excluded_candidate_ids]
        if len(candidate_ids) >= 2:
            filtered.append(candidate_ids)
    return filtered
