from __future__ import annotations

from typing import Any

from validator.judge_v1.runner import build_intrinsic_claim_rows


def build_intrinsic_source_rows(paper_output: dict[str, Any]) -> list[dict[str, Any]]:
    rows = build_intrinsic_claim_rows(paper_output)
    extraction_mode = extraction_mode_from_output(paper_output)
    for row in rows:
        row.setdefault("gold_match_status", "")
        row.setdefault("extraction_mode", extraction_mode)
    return rows


def build_candidate_missing_claim_rows(
    *,
    paper_output: dict[str, Any],
    audit_rows: list[dict[str, Any]],
    llm_audits: list[dict[str, Any]] | None = None,
    missing_claims_audit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not missing_claims_audit:
        return []
    paper = paper_output.get("paper") if isinstance(paper_output.get("paper"), dict) else {}
    paper_id = str(paper.get("paper_id", "") or "")
    candidates = missing_claims_audit.get("candidate_missing_claims")
    if not isinstance(candidates, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "paper_id": paper_id,
                "extraction_run_id": "",
                "candidate_claim_text": item.get("candidate_claim_text", ""),
                "candidate_subject": item.get("candidate_subject", ""),
                "candidate_predicate": item.get("candidate_predicate", ""),
                "candidate_object": item.get("candidate_object", ""),
                "source_span_ids": item.get("source_span_ids", []),
                "confidence": item.get("confidence", 0.0),
                "missing_reason": item.get("missing_reason", ""),
            }
        )
    return rows


def build_weak_or_unsupported_claim_rows(
    *,
    audit_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_by_claim_id = {
        str(row.get("claim_id", "")).strip(): row
        for row in source_rows
        if str(row.get("claim_id", "")).strip()
    }
    for row in audit_rows:
        status = str(row.get("source_support_status", "")).strip()
        evidence_score = _score(row.get("evidence_evaluation_score"))
        if status == "supported" and evidence_score >= 0.7:
            continue
        source_span_ids = []
        source = source_by_claim_id.get(str(row.get("claim_id", "")).strip(), {})
        metadata = source.get("extractor_metadata_json")
        if isinstance(metadata, dict):
            source_span_ids = metadata.get("source_span_ids") or []
        rows.append(
            {
                "paper_id": row.get("paper_id", ""),
                "extraction_run_id": row.get("extraction_run_id", ""),
                "claim_id": row.get("claim_id", ""),
                "claim_text": row.get("claim_text", ""),
                "source_span_ids": source_span_ids,
                "source_support_status": status or "uncertain",
                "support_comment": row.get("source_support_comment", "") or row.get("evidence_evaluation_comment", ""),
            }
        )
    return rows


def build_intrinsic_mode_summary(
    *,
    audit_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    candidate_missing_rows: list[dict[str, Any]],
    weak_or_unsupported_rows: list[dict[str, Any]],
    missing_claims_audit: dict[str, Any] | None = None,
    extraction_mode: str = "",
) -> dict[str, Any]:
    accuracy_score = _mean(row.get("accurate_extraction_score") for row in audit_rows)
    evidence_score = _mean(row.get("evidence_evaluation_score") for row in audit_rows)
    normalized_mode = extraction_mode or _first_extraction_mode(source_rows)
    coverage_target = (
        "abstract claims with evidence linked from the full paper"
        if normalized_mode == "abstract-full-paper"
        else "important claims across relevant paper sections"
    )
    coverage_score = None
    coverage_comment = (
        "Not scored in intrinsic mode without a gold set or full-paper missing-claim discovery pass. "
        f"Complete coverage means whether the run extracted all {coverage_target}."
    )
    if missing_claims_audit is not None:
        missing_weight = sum(_score(row.get("confidence")) for row in candidate_missing_rows)
        denominator = len(source_rows) + missing_weight
        coverage_score = round(len(source_rows) / denominator, 3) if denominator else 0.0
        coverage_comment = (
            f"Missing-claim discovery for {normalized_mode or 'unknown'} mode found "
            f"{len(candidate_missing_rows)} candidate missing claims. "
            f"{str(missing_claims_audit.get('coverage_comment') or '').strip()}"
        ).strip()
    return {
        "n_candidate_missing_claims": len(candidate_missing_rows),
        "n_weak_or_unsupported_claims": len(weak_or_unsupported_rows),
        "complete_coverage_score": coverage_score,
        "complete_coverage_comment": coverage_comment,
        "accurate_extraction_score": accuracy_score,
        "accurate_extraction_comment": (
            "Mean source-existence score over claims with linked evidence; checks that claim text and evidence text "
            "are present and source-grounded."
        ),
        "evidence_evaluation_score": evidence_score,
        "evidence_evaluation_comment": (
            f"Mean claim-evidence link-validity score over extracted claims; "
            f"{len(weak_or_unsupported_rows)} claims flagged as weak, unlinked, or uncertain."
        ),
        "comments": (
            f"Intrinsic run audit for {normalized_mode or 'unknown'} mode. "
            "Deterministic coverage cannot fully prove no claims were missed."
        ),
    }


def extraction_mode_from_output(paper_output: dict[str, Any]) -> str:
    mode = paper_output.get("pipeline_mode") or paper_output.get("extraction_mode")
    if mode:
        return str(mode)
    manifest = paper_output.get("manifest") if isinstance(paper_output.get("manifest"), dict) else {}
    if manifest.get("extraction_mode"):
        return str(manifest.get("extraction_mode"))
    return "section-local"


def _first_extraction_mode(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        mode = str(row.get("extraction_mode") or "").strip()
        if mode:
            return mode
    return ""


def _score(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _mean(values: Any) -> float:
    scores = [_score(value) for value in values]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 3)
