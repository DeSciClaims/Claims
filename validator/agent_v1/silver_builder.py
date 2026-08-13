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
    excluded_candidate_ids: set[str] | None = None,
) -> SilverRecord:
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    units_by_key: dict[tuple[str, str], SilverUnit] = {}
    invalid_candidates: list[InvalidMinerCandidate] = []
    reference_errors: list[ReferenceError] = []
    seen_invalid_candidates: set[tuple[str, str | None]] = set()
    seen_reference_errors: set[str] = set()

    accepted_candidate_ids: set[str] = set()
    accepted_bronze_ids: set[str] = set()
    rejected_bronze_ids: set[str] = set()
    for decision in decisions:
        for candidate_id in decision.accepted_candidate_ids:
            accepted_candidate_ids.add(candidate_id)
            candidate = candidates_by_id.get(candidate_id)
            if candidate and candidate.origin == "bronze":
                accepted_bronze_ids.add(candidate_id)
        for candidate_id in decision.valid_alternative_candidate_ids:
            accepted_candidate_ids.add(candidate_id)
        for candidate_id in decision.rejected_candidate_ids:
            candidate = candidates_by_id.get(candidate_id)
            if candidate and candidate.origin == "bronze":
                rejected_bronze_ids.add(candidate_id)
    excluded_ids = set(excluded_candidate_ids or set()) - accepted_candidate_ids
    filtered_equivalent_candidate_groups = _filter_equivalence_groups(
        equivalent_candidate_groups or [],
        excluded_candidate_ids=excluded_ids | (rejected_bronze_ids - accepted_bronze_ids),
    )
    equivalence_group_by_candidate_id = _equivalence_group_by_candidate_id(filtered_equivalent_candidate_groups)

    for candidate in candidates:
        if candidate.origin != "bronze":
            continue
        if candidate.candidate_id in excluded_ids:
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
        silver_units=_dedupe_units_by_candidate_set(list(units_by_key.values())),
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
    candidate_ids = sorted(set(_expanded_candidate_ids(accepted, equivalence_group_by_candidate_id)))
    effective_accepted = [candidate for candidate in accepted if candidate.candidate_id in candidate_ids]
    if not effective_accepted:
        return
    primary = _primary_candidate(effective_accepted)
    if any(candidate_id.startswith("bronze:") for candidate_id in candidate_ids):
        scoring_mode = "required"
        required_for_completeness = True
    key = _unit_key(
        effective_accepted,
        scoring_mode=scoring_mode,
        equivalence_group_by_candidate_id=equivalence_group_by_candidate_id,
    )
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
            source_quotes=_candidate_source_quotes(candidate_ids, candidates_by_id),
            adjudication_case_ids=[case_id] if case_id else [],
            scoring_mode=scoring_mode,  # type: ignore[arg-type]
            metadata={
                "candidate_provenance": _candidate_provenance(candidate_ids, candidates_by_id),
                "evidence_records": _candidate_evidence_records(candidate_ids, candidates_by_id),
            },
        )
        units_by_key[key] = unit
        return
    unit.required_for_completeness = unit.required_for_completeness or required_for_completeness
    unit.equivalent_candidate_ids = sorted(set(unit.equivalent_candidate_ids + candidate_ids))
    unit.evidence_ids = sorted(set(unit.evidence_ids + _candidate_evidence_ids(candidate_ids, candidates_by_id)))
    unit.source_span_ids = sorted(set(unit.source_span_ids + _candidate_source_span_ids(candidate_ids, candidates_by_id)))
    unit.source_quotes = sorted(set(unit.source_quotes + _candidate_source_quotes(candidate_ids, candidates_by_id)))
    unit.metadata["candidate_provenance"] = _merge_unique_dicts(
        unit.metadata.get("candidate_provenance"),
        _candidate_provenance(candidate_ids, candidates_by_id),
        key="candidate_id",
    )
    unit.metadata["evidence_records"] = _merge_unique_dicts(
        unit.metadata.get("evidence_records"),
        _candidate_evidence_records(candidate_ids, candidates_by_id),
        key="evidence_id",
    )
    if case_id:
        unit.adjudication_case_ids = sorted(set(unit.adjudication_case_ids + [case_id]))


def _dedupe_units_by_candidate_set(units: list[SilverUnit]) -> list[SilverUnit]:
    units_by_candidate_set: dict[tuple[str, ...], SilverUnit] = {}
    ordered_keys: list[tuple[str, ...]] = []
    for unit in units:
        key = tuple(sorted(set(unit.equivalent_candidate_ids))) or (unit.silver_unit_id,)
        existing = units_by_candidate_set.get(key)
        if existing is None:
            units_by_candidate_set[key] = unit
            ordered_keys.append(key)
            continue
        existing.required_for_completeness = existing.required_for_completeness or unit.required_for_completeness
        if existing.required_for_completeness or unit.scoring_mode == "required":
            existing.scoring_mode = "required"
        existing.importance = _max_importance(existing.importance, unit.importance)  # type: ignore[assignment]
        existing.evidence_ids = sorted(set(existing.evidence_ids + unit.evidence_ids))
        existing.source_span_ids = sorted(set(existing.source_span_ids + unit.source_span_ids))
        existing.source_quotes = sorted(set(existing.source_quotes + unit.source_quotes))
        existing.adjudication_case_ids = sorted(set(existing.adjudication_case_ids + unit.adjudication_case_ids))
        existing.metadata["candidate_provenance"] = _merge_unique_dicts(
            existing.metadata.get("candidate_provenance"),
            unit.metadata.get("candidate_provenance") if isinstance(unit.metadata.get("candidate_provenance"), list) else [],
            key="candidate_id",
        )
        existing.metadata["evidence_records"] = _merge_unique_dicts(
            existing.metadata.get("evidence_records"),
            unit.metadata.get("evidence_records") if isinstance(unit.metadata.get("evidence_records"), list) else [],
            key="evidence_id",
        )
    return [units_by_candidate_set[key] for key in ordered_keys]


def _max_importance(left: str, right: str) -> str:
    ranks = {"minor": 0, "supporting": 1, "central": 2}
    return left if ranks.get(left, 0) >= ranks.get(right, 0) else right


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
    if scoring_mode == "required" and (bronze_ids or group_bronze_ids):
        if equivalence_ids:
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


def _candidate_source_quotes(candidate_ids: list[str], candidates_by_id: dict[str, ComparisonCandidate]) -> list[str]:
    source_quotes: list[str] = []
    for candidate_id in candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if candidate:
            source_quotes.extend(candidate.source_quotes)
            for evidence in _candidate_evidence_records([candidate_id], candidates_by_id):
                for ref in evidence.get("source_refs", []):
                    quote = ref.get("quote") if isinstance(ref, dict) else None
                    if isinstance(quote, str) and quote.strip():
                        source_quotes.append(quote.strip())
    return sorted({quote for quote in source_quotes if quote})


def _candidate_evidence_records(candidate_ids: list[str], candidates_by_id: dict[str, ComparisonCandidate]) -> list[dict]:
    records: list[dict] = []
    for candidate_id in candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if not candidate:
            continue
        evidence_records = candidate.metadata.get("evidence_records") if isinstance(candidate.metadata, dict) else None
        if isinstance(evidence_records, list):
            records.extend(record for record in evidence_records if isinstance(record, dict))
    return _merge_unique_dicts([], records, key="evidence_id")


def _candidate_provenance(candidate_ids: list[str], candidates_by_id: dict[str, ComparisonCandidate]) -> list[dict]:
    rows: list[dict] = []
    for candidate_id in candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if not candidate:
            continue
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "origin": candidate.origin,
                "miner_id": candidate.miner_id,
                "record_id": candidate.record_id,
                "statement": candidate.statement,
                "evidence_ids": candidate.evidence_ids,
                "source_span_ids": candidate.source_span_ids,
            }
        )
    return rows


def _merge_unique_dicts(existing: object, new_rows: list[dict], *, key: str) -> list[dict]:
    merged: dict[str, dict] = {}
    if isinstance(existing, list):
        for row in existing:
            if isinstance(row, dict):
                row_key = str(row.get(key) or "")
                if row_key:
                    merged[row_key] = row
    for row in new_rows:
        row_key = str(row.get(key) or "")
        if row_key:
            merged[row_key] = row
    return [merged[item_key] for item_key in sorted(merged)]


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
    explicit_bronze_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.origin == "bronze"
    }
    candidate_ids: list[str] = []
    for candidate in candidates:
        group = equivalence_group_by_candidate_id.get(candidate.candidate_id, (candidate.candidate_id,))
        group_bronze_ids = {candidate_id for candidate_id in group if candidate_id.startswith("bronze:")}
        if explicit_bronze_ids and group_bronze_ids and not group_bronze_ids.issubset(explicit_bronze_ids):
            if candidate.origin == "bronze":
                candidate_ids.append(candidate.candidate_id)
            continue
        candidate_ids.extend(group)
    return candidate_ids


def _equivalence_group_by_candidate_id(groups: list[list[str]]) -> dict[str, tuple[str, ...]]:
    parent: dict[str, str] = {}
    bronze_ids_by_root: dict[str, set[str]] = {}

    def find(candidate_id: str) -> str:
        if candidate_id not in parent:
            parent[candidate_id] = candidate_id
            bronze_ids_by_root[candidate_id] = {candidate_id} if candidate_id.startswith("bronze:") else set()
        if parent[candidate_id] != candidate_id:
            parent[candidate_id] = find(parent[candidate_id])
        return parent[candidate_id]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        left_bronze_ids = bronze_ids_by_root.get(left_root, set())
        right_bronze_ids = bronze_ids_by_root.get(right_root, set())
        if left_bronze_ids and right_bronze_ids and left_bronze_ids != right_bronze_ids:
            return
        parent[right_root] = left_root
        bronze_ids_by_root[left_root] = left_bronze_ids | right_bronze_ids
        bronze_ids_by_root.pop(right_root, None)

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
