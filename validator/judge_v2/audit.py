from __future__ import annotations

import csv
import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miner.section_context_v1.profiles import validate_claim_against_profile


AUDIT_FIELDNAMES = [
    "paper_id",
    "extraction_run_id",
    "claim_id",
    "claim_profile",
    "claim_text",
    "subject",
    "predicate",
    "object",
    "audit_source",
    "audit_mode",
    "audit_method",
    "audit_version",
    "audit_status",
    "overall_score",
    "complete_coverage_score",
    "complete_coverage_comment",
    "accurate_extraction_score",
    "accurate_extraction_comment",
    "evidence_evaluation_score",
    "evidence_evaluation_comment",
    "primary_issue",
    "issue_tags",
    "missing_elements",
    "suggested_corrections_json",
    "comments",
    "gold_group_id",
    "gold_source_quote",
    "gold_match_score",
    "created_at",
]


def write_audit_records(output_path: Path, rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in AUDIT_FIELDNAMES})


def build_audit_records(
    rows: list[dict[str, Any]],
    *,
    audit_mode: str,
    audit_method: str = "deterministic",
    extraction_run_id: str,
    audit_version: str = "v2",
    llm_audits: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        build_audit_record(
            row,
            audit_mode=audit_mode,
            audit_method=audit_method,
            extraction_run_id=extraction_run_id,
            audit_version=audit_version,
            llm_audit=llm_audits[index] if llm_audits and index < len(llm_audits) else None,
        )
        for index, row in enumerate(rows)
    ]


def build_audit_record(
    row: dict[str, Any],
    *,
    audit_mode: str,
    audit_method: str,
    extraction_run_id: str,
    audit_version: str,
    llm_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_tags: list[str] = []
    missing_elements: list[str] = []
    suggested_corrections: dict[str, Any] = {}

    coverage_score, coverage_comment = _score_complete_coverage(row, missing_elements, issue_tags)
    accuracy_score, accuracy_comment = _score_accuracy(row, issue_tags, suggested_corrections)
    evidence_score, evidence_comment = _score_evidence(row, issue_tags, missing_elements)

    if audit_mode == "gold_comparison":
        gold_score, gold_comment, gold_issues, gold_corrections = _score_gold_alignment(row)
        accuracy_score = round((accuracy_score + gold_score) / 2, 3)
        accuracy_comment = _join_comment(accuracy_comment, gold_comment)
        issue_tags.extend(gold_issues)
        if gold_corrections:
            suggested_corrections.update(gold_corrections)

    overall_score = round((coverage_score + accuracy_score + evidence_score) / 3, 3)
    primary_issue = issue_tags[0] if issue_tags else ""
    audit_status = _status_for_score(overall_score, primary_issue)
    if llm_audit:
        coverage_score = _coalesce_score(llm_audit.get("complete_coverage_score"), coverage_score)
        coverage_comment = str(llm_audit.get("complete_coverage_comment") or coverage_comment)
        accuracy_score = _coalesce_score(llm_audit.get("accurate_extraction_score"), accuracy_score)
        accuracy_comment = str(llm_audit.get("accurate_extraction_comment") or accuracy_comment)
        evidence_score = _coalesce_score(llm_audit.get("evidence_evaluation_score"), evidence_score)
        evidence_comment = str(llm_audit.get("evidence_evaluation_comment") or evidence_comment)
        overall_score = _coalesce_score(llm_audit.get("overall_score"), round((coverage_score + accuracy_score + evidence_score) / 3, 3))
        audit_status = str(llm_audit.get("audit_status") or _status_for_score(overall_score, primary_issue))
        llm_primary_issue = str(llm_audit.get("primary_issue") or "").strip()
        if llm_primary_issue:
            primary_issue = llm_primary_issue
        issue_tags = _merge_unique(issue_tags, [str(item) for item in llm_audit.get("issue_tags", []) if str(item).strip()])
        missing_elements = _merge_unique(missing_elements, [str(item) for item in llm_audit.get("missing_elements", []) if str(item).strip()])
        llm_corrections = llm_audit.get("suggested_corrections_json")
        if isinstance(llm_corrections, dict):
            suggested_corrections.update(llm_corrections)

    return {
        "paper_id": row.get("paper_id", ""),
        "extraction_run_id": extraction_run_id,
        "claim_id": row.get("claim_id", ""),
        "claim_profile": row.get("claim_profile", ""),
        "claim_text": row.get("selected_claim_text", ""),
        "subject": row.get("selected_subject", ""),
        "predicate": row.get("selected_predicate", ""),
        "object": row.get("selected_object", ""),
        "audit_source": "validator",
        "audit_mode": audit_mode,
        "audit_method": audit_method,
        "audit_version": audit_version,
        "audit_status": audit_status,
        "overall_score": overall_score,
        "complete_coverage_score": coverage_score,
        "complete_coverage_comment": coverage_comment,
        "accurate_extraction_score": accuracy_score,
        "accurate_extraction_comment": accuracy_comment,
        "evidence_evaluation_score": evidence_score,
        "evidence_evaluation_comment": evidence_comment,
        "primary_issue": primary_issue,
        "issue_tags": issue_tags,
        "missing_elements": sorted(set(missing_elements)),
        "suggested_corrections_json": suggested_corrections,
        "comments": str((llm_audit or {}).get("comments") or _overall_comment(issue_tags)),
        "gold_group_id": row.get("group_id", "") if audit_mode == "gold_comparison" else "",
        "gold_source_quote": row.get("source_quote", "") if audit_mode == "gold_comparison" else "",
        "gold_match_score": row.get("match_score", "") if audit_mode == "gold_comparison" else "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _score_complete_coverage(
    row: dict[str, Any],
    missing_elements: list[str],
    issue_tags: list[str],
) -> tuple[float, str]:
    checks = {
        "claim_text": bool(str(row.get("selected_claim_text", "")).strip()),
        "subject": bool(str(row.get("selected_subject", "")).strip()),
        "predicate": bool(str(row.get("selected_predicate", "")).strip()),
        "object": bool(str(row.get("selected_object", "")).strip()),
        "source_span_ids": bool((row.get("extractor_metadata_json") or {}).get("source_span_ids")),
    }
    context = row.get("extracted_context_json") if isinstance(row.get("extracted_context_json"), dict) else {}
    details = row.get("extracted_details_json") if isinstance(row.get("extracted_details_json"), dict) else {}
    checks["context_or_details"] = bool(context or details)

    for key, present in checks.items():
        if not present:
            missing_elements.append(key)

    score = round(sum(1 for present in checks.values() if present) / len(checks), 3)
    if score < 1:
        issue_tags.append("incomplete_coverage")
    return score, _coverage_comment(checks)


def _score_accuracy(
    row: dict[str, Any],
    issue_tags: list[str],
    suggested_corrections: dict[str, Any],
) -> tuple[float, str]:
    claim = {
        "claim_profile": row.get("claim_profile", ""),
        "claim_text": row.get("selected_claim_text", ""),
        "subject": {"value": row.get("selected_subject", "")},
        "predicate": {"value": row.get("selected_predicate", "")},
        "object": {"value": row.get("selected_object", "")},
        "context": row.get("extracted_context_json", {}) if isinstance(row.get("extracted_context_json"), dict) else {},
        "details": row.get("extracted_details_json", {}) if isinstance(row.get("extracted_details_json"), dict) else {},
    }
    errors = validate_claim_against_profile(claim)
    if errors:
        issue_tags.extend(f"profile_validation:{error}" for error in errors)
        suggested_corrections["profile_validation_errors"] = errors
    score = max(0.0, round(1.0 - min(len(errors), 4) * 0.25, 3))
    comment = "No profile-shape validation errors." if not errors else "Profile validation errors: " + "; ".join(errors)
    return score, comment


def _score_evidence(
    row: dict[str, Any],
    issue_tags: list[str],
    missing_elements: list[str],
) -> tuple[float, str]:
    linked_ids = [item.strip() for item in str(row.get("linked_evidence_ids", "")).split(";") if item.strip()]
    evidence_items = row.get("group_evidence_items_json")
    if not isinstance(evidence_items, list):
        evidence_items = []
    links = row.get("group_links_json")
    if not isinstance(links, list):
        links = []

    score = 1.0
    comments: list[str] = []
    if not linked_ids:
        score -= 0.45
        issue_tags.append("missing_evidence_links")
        missing_elements.append("linked_evidence_ids")
        comments.append("No linked evidence IDs.")
    if not evidence_items:
        score -= 0.35
        issue_tags.append("missing_evidence_items")
        missing_elements.append("evidence_items")
        comments.append("No linked evidence item payload.")
    if linked_ids and len(evidence_items) < len(linked_ids):
        score -= 0.2
        issue_tags.append("incomplete_evidence_payload")
        comments.append("Some linked evidence IDs do not have evidence item payloads.")
    if not links:
        score -= 0.2
        issue_tags.append("missing_claim_evidence_links")
        missing_elements.append("claim_evidence_links")
        comments.append("No claim-evidence link payload.")

    score = round(max(0.0, score), 3)
    return score, " ".join(comments) if comments else "Linked evidence is present."


def _score_gold_alignment(row: dict[str, Any]) -> tuple[float, str, list[str], dict[str, Any]]:
    gold = row.get("gold_claim_json")
    if not isinstance(gold, dict):
        return 0.5, "No gold claim fields were available for direct comparison.", ["missing_gold_claim_fields"], {}

    comparisons = {
        "claim_text": _text_similarity(row.get("selected_claim_text", ""), gold.get("claim_text", "")),
        "subject": _text_similarity(row.get("selected_subject", ""), gold.get("subject", "")),
        "predicate": _text_similarity(row.get("selected_predicate", ""), gold.get("predicate", "")),
        "object": _text_similarity(row.get("selected_object", ""), gold.get("object", "")),
    }
    available = {key: value for key, value in comparisons.items() if value is not None}
    if not available:
        return 0.5, "Gold fields were empty.", ["missing_gold_claim_fields"], {}

    score = round(sum(available.values()) / len(available), 3)
    issues: list[str] = []
    corrections: dict[str, Any] = {}
    for key, similarity in available.items():
        if similarity < 0.72:
            issues.append(f"gold_mismatch:{key}")
            corrections[key] = gold.get(key, "")
    match_score = _coerce_float(row.get("match_score"))
    if match_score is None or match_score < 0.45:
        issues.append("weak_gold_match")
        score = min(score, 0.4)
    comment = "Gold field alignment score based on claim text and SPO similarity."
    if issues:
        comment += " Mismatches: " + ", ".join(issues)
    return score, comment, issues, corrections


def _coverage_comment(checks: dict[str, bool]) -> str:
    missing = [key for key, present in checks.items() if not present]
    if not missing:
        return "Claim packet has core fields, qualifiers/payload, and source provenance."
    return "Missing or empty: " + ", ".join(missing)


def _status_for_score(score: float, primary_issue: str) -> str:
    if score >= 0.85 and not primary_issue:
        return "accepted"
    if score >= 0.55:
        return "needs_correction"
    return "rejected"


def _overall_comment(issue_tags: list[str]) -> str:
    if not issue_tags:
        return "No deterministic audit issues found."
    return "Deterministic audit found: " + ", ".join(issue_tags[:6])


def _join_comment(left: str, right: str) -> str:
    return f"{left} {right}".strip()


def _coalesce_score(value: Any, fallback: float) -> float:
    score = _coerce_float(value)
    if score is None:
        return fallback
    return round(min(1.0, max(0.0, score)), 3)


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _text_similarity(left: Any, right: Any) -> float | None:
    left_norm = _normalize_text(str(left or ""))
    right_norm = _normalize_text(str(right or ""))
    if not left_norm and not right_norm:
        return None
    if not left_norm or not right_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens) / max(len(right_tokens), 1)
    return round((ratio * 0.65) + (overlap * 0.35), 3)


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\s]+", " ", value.lower()).strip()


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*left, *right]:
        if item and item not in merged:
            merged.append(item)
    return merged


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
