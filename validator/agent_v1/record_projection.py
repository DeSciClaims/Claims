from __future__ import annotations

import re
from typing import Any

from .comparison_models import ComparisonCandidate, CandidateOrigin, Importance


def project_agent_artifact(
    artifact: dict[str, Any],
    *,
    origin: CandidateOrigin,
    miner_id: str | None = None,
    default_importance: Importance = "supporting",
) -> list[ComparisonCandidate]:
    paper = artifact.get("paper") if isinstance(artifact.get("paper"), dict) else {}
    logic = artifact.get("logic") if isinstance(artifact.get("logic"), dict) else {}
    claims = logic.get("claims") if isinstance(logic.get("claims"), list) else []
    evidence_layer = artifact.get("evidence") if isinstance(artifact.get("evidence"), dict) else {}
    evidence_records = evidence_layer.get("records") if isinstance(evidence_layer.get("records"), list) else []
    evidence_by_id = {
        _text(record.get("evidence_id")): record
        for record in evidence_records
        if isinstance(record, dict) and _text(record.get("evidence_id"))
    }
    paper_id = _text(paper.get("paper_id")) or None
    projected: list[ComparisonCandidate] = []

    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        record_id = _text(claim.get("claim_id")) or f"claim_{index + 1}"
        statement = _text(claim.get("statement"))
        if not statement:
            continue
        source_refs = [ref for ref in claim.get("sources", []) if isinstance(ref, dict)] if isinstance(claim.get("sources"), list) else []
        span_ids: list[str] = []
        quotes: list[str] = []
        for ref in source_refs:
            span_ids.extend(_text_list(ref.get("span_ids")))
            quote = _text(ref.get("quote"))
            if quote:
                quotes.append(quote)
        evidence_ids = _text_list(claim.get("evidence_ids"))
        linked_evidence = [
            _candidate_evidence_record(record)
            for evidence_id in evidence_ids
            if (record := evidence_by_id.get(evidence_id))
        ]
        projected.append(
            ComparisonCandidate(
                candidate_id=_candidate_id(origin=origin, miner_id=miner_id, record_id=record_id),
                paper_id=paper_id,
                origin=origin,
                miner_id=miner_id,
                record_id=record_id,
                statement=statement,
                normalized_statement=normalize_statement(statement),
                qualifier=_text(claim.get("conditions")) or None,
                evidence_ids=evidence_ids,
                source_span_ids=sorted(set(span_ids)),
                source_quotes=quotes,
                # Importance is assigned later on canonical Silver units. Miner or
                # reference artifact metadata must not control scoring weights.
                importance=default_importance,
                metadata={
                    "source_claim": claim,
                    "source_claim_metadata_importance": _metadata_importance(claim.get("metadata")),
                    "evidence_records": linked_evidence,
                },
            )
        )
    return projected


def normalize_statement(value: str) -> str:
    normalized = value.casefold()
    normalized = re.sub(r"[^a-z0-9\s]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _candidate_id(*, origin: CandidateOrigin, miner_id: str | None, record_id: str) -> str:
    if origin == "bronze":
        return f"bronze:{record_id}"
    return f"miner:{miner_id or 'unknown'}:{record_id}"


def _metadata_importance(metadata: Any) -> str:
    if isinstance(metadata, dict):
        value = _text(metadata.get("importance")).lower()
        if value in {"central", "supporting", "minor"}:
            return value
    return ""


def _candidate_evidence_record(record: dict[str, Any]) -> dict[str, Any]:
    source_refs = record.get("source_refs") if isinstance(record.get("source_refs"), list) else []
    return {
        "evidence_id": _text(record.get("evidence_id")),
        "title": _text(record.get("title")),
        "role": _text(record.get("role")),
        "summary": _text(record.get("summary")),
        "evidence_method": _text(record.get("evidence_method")),
        "outcome_type": _text(record.get("outcome_type")),
        "presentation_type": _text(record.get("presentation_type")),
        "source_refs": [
            {
                "source_id": _text(ref.get("source_id")),
                "source_type": _text(ref.get("source_type")),
                "span_ids": _text_list(ref.get("span_ids")),
                "quote": _text(ref.get("quote")),
                "role": _text(ref.get("role")),
            }
            for ref in source_refs
            if isinstance(ref, dict)
        ],
        "linked_claim_ids": _text_list(record.get("linked_claim_ids")),
    }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
