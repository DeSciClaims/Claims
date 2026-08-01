from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def summarize_agent_artifact(
    artifact: Any,
    *,
    max_claims: int = 80,
    max_evidence_items: int = 80,
) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        return {}
    paper = _record(artifact.get("paper"))
    logic = _record(artifact.get("logic"))
    evidence = _record(artifact.get("evidence"))
    claims = _rows(logic.get("claims"))
    evidence_items = [
        *_rows(evidence.get("items")),
        *_rows(evidence.get("evidence_items")),
        *_rows(evidence.get("records")),
    ]
    return {
        "schema": "claims_artifact_summary_v1",
        "paper_id": _text(paper.get("paper_id")),
        "title": _text(paper.get("title") or paper.get("paper_id")),
        "claim_count": len(claims),
        "evidence_count": len(evidence_items),
        "claims": [
            _summarize_claim(claim, index)
            for index, claim in enumerate(claims[:max_claims], start=1)
        ],
        "evidence_items": [
            _summarize_evidence_item(item, index)
            for index, item in enumerate(evidence_items[:max_evidence_items], start=1)
        ],
    }


def summarize_agent_artifact_path(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return summarize_agent_artifact(payload)


def _summarize_claim(claim: dict[str, Any], index: int) -> dict[str, Any]:
    metadata = _record(claim.get("metadata"))
    return {
        "claim_id": _text(claim.get("claim_id")) or f"C{index:02d}",
        "statement": _text(claim.get("statement") or claim.get("claim_text")),
        "qualifier": _text(claim.get("conditions") or claim.get("qualifier") or metadata.get("qualifier")),
        "status": _text(claim.get("status")),
        "evidence_ids": _string_array(claim.get("evidence_ids") or claim.get("proof")),
        "source_span_ids": _source_span_ids(claim),
    }


def _summarize_evidence_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    refs = _rows(item.get("source_refs") or item.get("sources"))
    first_ref = refs[0] if refs else {}
    source_span_ids = set(_string_array(item.get("source_span_ids") or item.get("span_ids")))
    for ref in refs:
        source_span_ids.update(_string_array(ref.get("span_ids") or ref.get("source_span_ids")))
    return {
        "evidence_id": _text(item.get("evidence_id") or item.get("id")) or f"EV{index:02d}",
        "title": _text(item.get("title")),
        "role": _text(item.get("role")),
        "summary": _text(item.get("summary") or item.get("description")),
        "quote": _text(item.get("quote") or item.get("source_quote") or first_ref.get("quote")),
        "source_span_ids": sorted(source_span_ids),
        "linked_claim_ids": _string_array(item.get("linked_claim_ids") or item.get("claim_ids")),
    }


def _source_span_ids(claim: dict[str, Any]) -> list[str]:
    span_ids = set(_string_array(claim.get("source_span_ids") or claim.get("span_ids")))
    for source in _rows(claim.get("sources") or claim.get("source_refs")):
        span_ids.update(_string_array(source.get("span_ids") or source.get("source_span_ids")))
    return sorted(span_ids)


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_array(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""
