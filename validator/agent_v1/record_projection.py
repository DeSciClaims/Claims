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
                evidence_ids=_text_list(claim.get("evidence_ids")),
                source_span_ids=sorted(set(span_ids)),
                source_quotes=quotes,
                importance=_importance(claim.get("metadata"), default_importance),
                metadata={"source_claim": claim},
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


def _importance(metadata: Any, default: Importance) -> Importance:
    if isinstance(metadata, dict):
        value = _text(metadata.get("importance")).lower()
        if value in {"central", "supporting", "minor"}:
            return value  # type: ignore[return-value]
    return default


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
