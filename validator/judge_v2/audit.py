from __future__ import annotations

import csv
import difflib
import json
import re
from collections import Counter
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
    "gold_match_status",
    "gold_claim_text",
    "gold_subject",
    "gold_predicate",
    "gold_object",
    "source_support_status",
    "source_support_comment",
    "created_at",
]

RUN_AUDIT_FIELDNAMES = [
    "paper_id",
    "extraction_run_id",
    "audit_source",
    "audit_mode",
    "audit_method",
    "audit_version",
    "audit_status",
    "n_claims",
    "n_accepted",
    "n_needs_correction",
    "n_rejected",
    "n_gold_claims",
    "n_gold_claims_matched",
    "n_gold_claims_missing",
    "n_extra_extracted_claims",
    "n_candidate_missing_claims",
    "n_weak_or_unsupported_claims",
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
    "comments",
    "created_at",
]

MISSING_GOLD_FIELDNAMES = [
    "paper_id",
    "extraction_run_id",
    "gold_group_id",
    "gold_claim_text",
    "gold_subject",
    "gold_predicate",
    "gold_object",
    "gold_source_quote",
    "importance",
    "missing_reason",
]

EXTRA_EXTRACTED_FIELDNAMES = [
    "paper_id",
    "extraction_run_id",
    "claim_id",
    "claim_text",
    "subject",
    "predicate",
    "object",
    "best_gold_match_score",
    "extra_reason",
]

CANDIDATE_MISSING_FIELDNAMES = [
    "paper_id",
    "extraction_run_id",
    "candidate_claim_text",
    "candidate_subject",
    "candidate_predicate",
    "candidate_object",
    "source_span_ids",
    "confidence",
    "missing_reason",
]

WEAK_OR_UNSUPPORTED_FIELDNAMES = [
    "paper_id",
    "extraction_run_id",
    "claim_id",
    "claim_text",
    "source_span_ids",
    "source_support_status",
    "support_comment",
]


def write_audit_records(output_path: Path, rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in AUDIT_FIELDNAMES})


def write_run_audit_record(output_path: Path, row: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUN_AUDIT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({key: _csv_value(row.get(key, "")) for key in RUN_AUDIT_FIELDNAMES})


def write_diagnostic_records(output_path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})


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


def build_run_audit_record(
    audit_rows: list[dict[str, Any]],
    *,
    paper_id: str,
    audit_mode: str,
    audit_method: str,
    extraction_run_id: str,
    audit_version: str = "v2",
    mode_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    summary = mode_summary or {}
    if not audit_rows:
        return {
            "paper_id": paper_id,
            "extraction_run_id": extraction_run_id,
            "audit_source": "validator",
            "audit_mode": audit_mode,
            "audit_method": audit_method,
            "audit_version": audit_version,
            "audit_status": "rejected",
            "n_claims": 0,
            "n_accepted": 0,
            "n_needs_correction": 0,
            "n_rejected": 0,
            "n_gold_claims": summary.get("n_gold_claims", ""),
            "n_gold_claims_matched": summary.get("n_gold_claims_matched", ""),
            "n_gold_claims_missing": summary.get("n_gold_claims_missing", ""),
            "n_extra_extracted_claims": summary.get("n_extra_extracted_claims", ""),
            "n_candidate_missing_claims": summary.get("n_candidate_missing_claims", ""),
            "n_weak_or_unsupported_claims": summary.get("n_weak_or_unsupported_claims", ""),
            "overall_score": 0.0,
            "complete_coverage_score": 0.0,
            "complete_coverage_comment": "No extracted claims were available to audit.",
            "accurate_extraction_score": 0.0,
            "accurate_extraction_comment": "No extracted claims were available to audit.",
            "evidence_evaluation_score": 0.0,
            "evidence_evaluation_comment": "No extracted claims were available to audit.",
            "primary_issue": "no_claims",
            "issue_tags": ["no_claims"],
            "missing_elements": ["claims"],
            "comments": "Run-level audit rejected because no claim audit rows were produced.",
            "created_at": created_at,
        }

    status_counts = Counter(str(row.get("audit_status") or "") for row in audit_rows)
    summary_coverage_score = _coerce_float(summary.get("complete_coverage_score"))
    coverage_score = summary_coverage_score
    accuracy_score = _mean_score(row.get("accurate_extraction_score") for row in audit_rows)
    evidence_score = _mean_score(row.get("evidence_evaluation_score") for row in audit_rows)
    if _coerce_float(summary.get("accurate_extraction_score")) is not None:
        accuracy_score = _coalesce_score(summary.get("accurate_extraction_score"), accuracy_score)
    if _coerce_float(summary.get("evidence_evaluation_score")) is not None:
        evidence_score = _coalesce_score(summary.get("evidence_evaluation_score"), evidence_score)
    overall_score = _mean_score([coverage_score, accuracy_score, evidence_score])
    issue_tags = _top_items(_flatten_list_field(row.get("issue_tags") for row in audit_rows))
    missing_elements = _top_items(_flatten_list_field(row.get("missing_elements") for row in audit_rows))
    primary_issue = issue_tags[0] if issue_tags else ""

    return {
        "paper_id": paper_id or str(audit_rows[0].get("paper_id", "")),
        "extraction_run_id": extraction_run_id,
        "audit_source": "validator",
        "audit_mode": audit_mode,
        "audit_method": audit_method,
        "audit_version": audit_version,
        "audit_status": _status_for_score(overall_score, primary_issue),
        "n_claims": len(audit_rows),
        "n_accepted": status_counts.get("accepted", 0),
        "n_needs_correction": status_counts.get("needs_correction", 0),
        "n_rejected": status_counts.get("rejected", 0),
        "n_gold_claims": summary.get("n_gold_claims", ""),
        "n_gold_claims_matched": summary.get("n_gold_claims_matched", ""),
        "n_gold_claims_missing": summary.get("n_gold_claims_missing", ""),
        "n_extra_extracted_claims": summary.get("n_extra_extracted_claims", ""),
        "n_candidate_missing_claims": summary.get("n_candidate_missing_claims", ""),
        "n_weak_or_unsupported_claims": summary.get("n_weak_or_unsupported_claims", ""),
        "overall_score": overall_score,
        "complete_coverage_score": coverage_score,
        "complete_coverage_comment": summary.get("complete_coverage_comment") or _run_dimension_comment("coverage", coverage_score, audit_rows),
        "accurate_extraction_score": accuracy_score,
        "accurate_extraction_comment": summary.get("accurate_extraction_comment") or _run_dimension_comment("accuracy", accuracy_score, audit_rows),
        "evidence_evaluation_score": evidence_score,
        "evidence_evaluation_comment": summary.get("evidence_evaluation_comment") or _run_dimension_comment("evidence", evidence_score, audit_rows),
        "primary_issue": primary_issue,
        "issue_tags": issue_tags,
        "missing_elements": missing_elements,
        "comments": summary.get("comments") or _run_overall_comment(len(audit_rows), status_counts, issue_tags),
        "created_at": created_at,
    }


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

    accuracy_score, accuracy_comment = _score_claim_extraction(row, missing_elements, issue_tags, suggested_corrections)
    evidence_score, evidence_comment = _score_evidence(row, issue_tags, missing_elements)
    gold_match_status = str(row.get("gold_match_status") or "").strip()
    coverage_score: float | None = None
    coverage_comment = ""

    if audit_mode == "gold_comparison":
        gold_score, gold_comment, gold_issues, gold_corrections = _score_gold_alignment(row)
        accuracy_score = round((accuracy_score + gold_score) / 2, 3)
        accuracy_comment = _join_comment(accuracy_comment, gold_comment)
        issue_tags.extend(gold_issues)
        if gold_corrections:
            suggested_corrections.update(gold_corrections)
        if gold_match_status == "missing_gold":
            accuracy_score = min(accuracy_score, 0.25)
            evidence_score = 0.0
            accuracy_comment = _join_comment(accuracy_comment, "No extracted claim matched this gold claim.")
            evidence_comment = "No extracted evidence can be evaluated for a missing gold claim."
            issue_tags.append("missing_gold_claim")
            missing_elements.append("matched_extracted_claim")
        elif gold_match_status == "extra_extracted":
            accuracy_score = min(accuracy_score, 0.4)
            accuracy_comment = _join_comment(accuracy_comment, "Extracted claim has no adequate gold match.")
            issue_tags.append("extra_extracted_claim")

    overall_score = round((accuracy_score + evidence_score) / 2, 3)
    primary_issue = issue_tags[0] if issue_tags else ""
    audit_status = _status_for_score(overall_score, primary_issue)
    if llm_audit:
        accuracy_score = _coalesce_score(llm_audit.get("accurate_extraction_score"), accuracy_score)
        accuracy_comment = str(llm_audit.get("accurate_extraction_comment") or accuracy_comment)
        evidence_score = _coalesce_score(llm_audit.get("evidence_evaluation_score"), evidence_score)
        evidence_comment = str(llm_audit.get("evidence_evaluation_comment") or evidence_comment)
        overall_score = _coalesce_score(llm_audit.get("overall_score"), round((accuracy_score + evidence_score) / 2, 3))
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
        "gold_match_status": gold_match_status if audit_mode == "gold_comparison" else "",
        "gold_claim_text": _gold_value(row, "claim_text") if audit_mode == "gold_comparison" else "",
        "gold_subject": _gold_value(row, "subject") if audit_mode == "gold_comparison" else "",
        "gold_predicate": _gold_value(row, "predicate") if audit_mode == "gold_comparison" else "",
        "gold_object": _gold_value(row, "object") if audit_mode == "gold_comparison" else "",
        "source_support_status": _source_support_status(evidence_score, row),
        "source_support_comment": evidence_comment,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _score_claim_extraction(
    row: dict[str, Any],
    missing_elements: list[str],
    issue_tags: list[str],
    suggested_corrections: dict[str, Any],
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
    missing_count = sum(1 for present in checks.values() if not present)
    if missing_count:
        issue_tags.append("claim_packet_field_missing")
    score = max(0.0, round(1.0 - min(len(errors), 3) * 0.2 - min(missing_count, 4) * 0.1, 3))
    comments = []
    if errors:
        comments.append("Profile validation errors: " + "; ".join(errors))
    if missing_count:
        comments.append(_coverage_comment(checks))
    if not comments:
        comments.append("No common claim extraction failure modes detected.")
    comment = " ".join(comments)
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


def _gold_value(row: dict[str, Any], key: str) -> str:
    gold = row.get("gold_claim_json")
    if isinstance(gold, dict):
        return str(gold.get(key, "") or "")
    return ""


def _source_support_status(evidence_score: float, row: dict[str, Any]) -> str:
    if str(row.get("gold_match_status") or "") == "missing_gold":
        return ""
    if evidence_score >= 0.85:
        return "supported"
    if evidence_score >= 0.55:
        return "partially_supported"
    if evidence_score > 0:
        return "uncertain"
    return "unsupported"


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


def _mean_score(values: Any) -> float:
    scores = [_coerce_float(value) for value in values]
    present = [score for score in scores if score is not None]
    if not present:
        return 0.0
    return round(sum(present) / len(present), 3)


def _flatten_list_field(values: Any) -> list[str]:
    flattened: list[str] = []
    for value in values:
        if isinstance(value, list):
            items = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                items = parsed if isinstance(parsed, list) else [value]
            except json.JSONDecodeError:
                items = re.split(r"[;,]", value)
        else:
            items = []
        for item in items:
            text = str(item).strip()
            if text:
                flattened.append(text)
    return flattened


def _top_items(items: list[str], *, limit: int = 12) -> list[str]:
    counts = Counter(items)
    return [item for item, _count in counts.most_common(limit)]


def _run_dimension_comment(name: str, score: float, rows: list[dict[str, Any]]) -> str:
    weak = [str(row.get("claim_id") or "") for row in rows if (_coerce_float(row.get(f"{_dimension_prefix(name)}_score")) or 0.0) < 0.7]
    if not weak:
        return f"Mean {name} score across {len(rows)} audited claims."
    return f"Mean {name} score across {len(rows)} audited claims. Lower-scoring claims: {', '.join(weak[:8])}."


def _dimension_prefix(name: str) -> str:
    if name == "coverage":
        return "complete_coverage"
    if name == "accuracy":
        return "accurate_extraction"
    return "evidence_evaluation"


def _run_overall_comment(claim_count: int, status_counts: Counter[str], issue_tags: list[str]) -> str:
    status_summary = (
        f"{status_counts.get('accepted', 0)} accepted, "
        f"{status_counts.get('needs_correction', 0)} need correction, "
        f"{status_counts.get('rejected', 0)} rejected"
    )
    if not issue_tags:
        return f"Run-level audit over {claim_count} claims: {status_summary}; no recurring issues found."
    return f"Run-level audit over {claim_count} claims: {status_summary}. Main recurring issues: {', '.join(issue_tags[:6])}."


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
