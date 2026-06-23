from __future__ import annotations

from typing import Any

from ..section_context_v1.schema_models import OntologyAnnotation, OntologyCandidate

from .contracts import OntologyContextV1Contracts
from .field_routing import route_sources
from .registry_client import OntologyRegistryClient, OntologyCandidateRow, normalize_ontology_text


def resolve_annotation(
    *,
    contracts: OntologyContextV1Contracts,
    registry_client: OntologyRegistryClient,
    raw_text: str,
    lookup_text: str | None,
    field_path: str,
    entity_type: str | None,
    claim_profile: str | None,
    top_k_per_source: int,
    fuzzy_limit_per_source: int,
) -> tuple[OntologyAnnotation, list[str], dict[str, Any]]:
    normalized_text = normalize_ontology_text(lookup_text or raw_text)
    routed_sources = route_sources(
        contracts=contracts,
        field_path=field_path,
        entity_type=entity_type,
        claim_profile=claim_profile,
    )
    if not normalized_text:
        return _unresolved_annotation(raw_text=raw_text, normalized_text=normalized_text, mapping_method="empty_text"), routed_sources, {}
    if not routed_sources:
        return _unresolved_annotation(raw_text=raw_text, normalized_text=normalized_text, mapping_method="no_route"), routed_sources, {}

    candidates: list[OntologyCandidate] = []
    resolution_metadata: dict[str, Any] = {"matching_strategy": "exact_then_fuzzy", "sources_considered": routed_sources}
    seen: set[tuple[str, str]] = set()

    exact_rows: list[OntologyCandidateRow] = []
    for source in routed_sources:
        exact_rows.extend(
            registry_client.fetch_alias_candidates_exact(
                ontology_source=source,
                normalized_text=normalized_text,
                limit=top_k_per_source,
            )
        )
    for row in exact_rows:
        key = (row.ontology_source, row.ontology_id)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            OntologyCandidate(
                ontology_source=row.ontology_source,
                ontology_id=row.ontology_id,
                ontology_label=row.ontology_label,
                match_type=f"exact_{row.alias_type}",
                confidence=1.0,
            )
        )

    if not candidates:
        fuzzy_rows: list[OntologyCandidateRow] = []
        for source in routed_sources:
            fuzzy_rows.extend(
                registry_client.fetch_alias_candidates_fuzzy(
                    ontology_source=source,
                    normalized_text=normalized_text,
                    limit=fuzzy_limit_per_source,
                )
            )
        scored = sorted(
            (_score_candidate(raw_text=raw_text, normalized_text=normalized_text, candidate=row) for row in fuzzy_rows),
            key=lambda item: item["score"],
            reverse=True,
        )
        for item in scored[: max(1, top_k_per_source * len(routed_sources))]:
            row = item["row"]
            key = (row.ontology_source, row.ontology_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                OntologyCandidate(
                    ontology_source=row.ontology_source,
                    ontology_id=row.ontology_id,
                    ontology_label=row.ontology_label,
                    match_type=f"fuzzy_{row.alias_type}",
                    confidence=item["score"],
                )
            )
        resolution_metadata["fuzzy_candidate_pool_size"] = len(fuzzy_rows)

    if not candidates:
        return (
            OntologyAnnotation(
                annotation_type="mapping",
                raw_text=raw_text,
                normalized_text=normalized_text,
                mapping_status="unresolved",
                candidate_mappings=[],
                selected_mapping=None,
                mapping_method="ontology_context_v1_no_match",
            ),
            routed_sources,
            resolution_metadata,
        )

    if len(candidates) == 1 and (candidates[0].confidence or 0.0) >= 0.99:
        selected = candidates[0]
        return (
            OntologyAnnotation(
                annotation_type="mapping",
                raw_text=raw_text,
                normalized_text=normalized_text,
                mapping_status="mapped",
                candidate_mappings=candidates,
                selected_mapping=selected,
                mapping_method="ontology_context_v1_exact_alias",
            ),
            routed_sources,
            resolution_metadata,
        )

    return (
        OntologyAnnotation(
            annotation_type="mapping",
            raw_text=raw_text,
            normalized_text=normalized_text,
            mapping_status="ambiguous",
            candidate_mappings=candidates,
            selected_mapping=None,
            mapping_method="ontology_context_v1_candidate_list",
        ),
        routed_sources,
        resolution_metadata,
    )


def _score_candidate(*, raw_text: str, normalized_text: str, candidate: OntologyCandidateRow) -> dict[str, Any]:
    alias_norm = normalize_ontology_text(candidate.alias_text)
    label_norm = normalize_ontology_text(candidate.ontology_label)
    score = 0.0
    if alias_norm == normalized_text or label_norm == normalized_text:
        score = 1.0
    elif alias_norm.startswith(normalized_text) or label_norm.startswith(normalized_text):
        score = 0.9
    elif normalized_text in alias_norm or normalized_text in label_norm:
        score = 0.8
    else:
        score = _token_overlap_score(normalized_text, alias_norm, label_norm)
    return {"row": candidate, "score": score}


def _token_overlap_score(normalized_text: str, alias_norm: str, label_norm: str) -> float:
    query_tokens = set(normalized_text.split())
    alias_tokens = set(alias_norm.split())
    label_tokens = set(label_norm.split())
    best_overlap = 0.0
    for candidate_tokens in (alias_tokens, label_tokens):
        if not query_tokens or not candidate_tokens:
            continue
        overlap = len(query_tokens & candidate_tokens) / len(query_tokens | candidate_tokens)
        best_overlap = max(best_overlap, overlap)
    return round(best_overlap, 3)


def _unresolved_annotation(*, raw_text: str, normalized_text: str, mapping_method: str) -> OntologyAnnotation:
    return OntologyAnnotation(
        annotation_type="mapping",
        raw_text=raw_text,
        normalized_text=normalized_text,
        mapping_status="unresolved",
        candidate_mappings=[],
        selected_mapping=None,
        mapping_method=mapping_method,
    )
