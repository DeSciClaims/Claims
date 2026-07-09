from __future__ import annotations

import difflib
import re
from typing import Any

from validator.judge_v1.review_data import ReviewedQuoteGroup
from validator.judge_v1.runner import build_intrinsic_claim_rows, match_group_to_claim


MISSING_MATCH_THRESHOLD = 0.45
PARTIAL_MATCH_THRESHOLD = 0.72


def build_gold_source_rows(
    *,
    paper_output: dict[str, Any],
    quote_groups: list[ReviewedQuoteGroup],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source_rows: list[dict[str, Any]] = []
    matched_claim_ids: set[str] = set()

    for group in quote_groups:
        row = _with_gold_fields(match_group_to_claim(group, paper_output), group)
        match_score = _score(row.get("match_score"))
        claim_id = str(row.get("claim_id", "")).strip()
        if not claim_id or match_score < MISSING_MATCH_THRESHOLD:
            row["gold_match_status"] = "missing_gold"
        elif match_score < PARTIAL_MATCH_THRESHOLD:
            row["gold_match_status"] = "partial"
            matched_claim_ids.add(claim_id)
        else:
            row["gold_match_status"] = "matched"
            matched_claim_ids.add(claim_id)
        source_rows.append(row)

    extra_rows: list[dict[str, Any]] = []
    for row in build_intrinsic_claim_rows(paper_output):
        claim_id = str(row.get("claim_id", "")).strip()
        if not claim_id or claim_id in matched_claim_ids:
            continue
        best_score = _best_gold_similarity(row, quote_groups)
        extra_row = dict(row)
        extra_row.update(
            {
                "group_id": "",
                "source_quote": "",
                "match_score": round(best_score, 4),
                "gold_claim_json": {},
                "gold_claims_json": [],
                "gold_match_status": "extra_extracted",
            }
        )
        extra_rows.append(extra_row)

    all_rows = [*source_rows, *extra_rows]
    return all_rows, build_missing_gold_rows(source_rows), build_extra_extracted_rows(extra_rows)


def build_missing_gold_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in source_rows:
        if row.get("gold_match_status") != "missing_gold":
            continue
        gold = row.get("gold_claim_json") if isinstance(row.get("gold_claim_json"), dict) else {}
        rows.append(
            {
                "paper_id": row.get("paper_id", ""),
                "extraction_run_id": row.get("extraction_run_id", ""),
                "gold_group_id": row.get("group_id", ""),
                "gold_claim_text": gold.get("claim_text", ""),
                "gold_subject": gold.get("subject", ""),
                "gold_predicate": gold.get("predicate", ""),
                "gold_object": gold.get("object", ""),
                "gold_source_quote": row.get("source_quote", ""),
                "importance": "",
                "missing_reason": "No extracted claim matched this reviewed/gold claim above threshold.",
            }
        )
    return rows


def build_extra_extracted_rows(extra_source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in extra_source_rows:
        rows.append(
            {
                "paper_id": row.get("paper_id", ""),
                "extraction_run_id": row.get("extraction_run_id", ""),
                "claim_id": row.get("claim_id", ""),
                "claim_text": row.get("selected_claim_text", ""),
                "subject": row.get("selected_subject", ""),
                "predicate": row.get("selected_predicate", ""),
                "object": row.get("selected_object", ""),
                "best_gold_match_score": row.get("match_score", ""),
                "extra_reason": "Extracted claim was not matched to any reviewed/gold claim.",
            }
        )
    return rows


def build_gold_mode_summary(
    *,
    audit_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    missing_gold_rows: list[dict[str, Any]],
    extra_extracted_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    total_gold = sum(1 for row in source_rows if row.get("gold_match_status") in {"matched", "partial", "missing_gold"})
    matched = sum(1 for row in source_rows if row.get("gold_match_status") == "matched")
    partial = sum(1 for row in source_rows if row.get("gold_match_status") == "partial")
    coverage_score = round((matched + (partial * 0.5)) / total_gold, 3) if total_gold else 0.0
    matched_audits = [
        row for row in audit_rows if row.get("gold_match_status") in {"matched", "partial"}
    ]
    accuracy_score = _mean(row.get("accurate_extraction_score") for row in matched_audits)
    evidence_score = _mean(row.get("evidence_evaluation_score") for row in matched_audits)
    issue_bits = []
    if missing_gold_rows:
        issue_bits.append(f"{len(missing_gold_rows)} missing gold claims")
    if extra_extracted_rows:
        issue_bits.append(f"{len(extra_extracted_rows)} extra extracted claims")
    comments = "Gold-comparison run audit."
    if issue_bits:
        comments += " " + "; ".join(issue_bits) + "."
    return {
        "n_gold_claims": total_gold,
        "n_gold_claims_matched": matched,
        "n_gold_claims_missing": len(missing_gold_rows),
        "n_extra_extracted_claims": len(extra_extracted_rows),
        "complete_coverage_score": coverage_score,
        "complete_coverage_comment": (
            f"Gold coverage: {matched} matched, {partial} partial, "
            f"{len(missing_gold_rows)} missing out of {total_gold} gold claims."
        ),
        "accurate_extraction_score": accuracy_score,
        "accurate_extraction_comment": "Mean extraction accuracy over matched and partially matched gold claims.",
        "evidence_evaluation_score": evidence_score,
        "evidence_evaluation_comment": "Mean evidence score over matched and partially matched gold claims.",
        "comments": comments,
    }


def _with_gold_fields(row: dict[str, Any], group: ReviewedQuoteGroup) -> dict[str, Any]:
    updated = dict(row)
    target_claims = []
    for reviewed in group.rows:
        target_claims.append(
            {
                "claim_text": reviewed.gold_claim_text,
                "subject": reviewed.gold_subject,
                "predicate": reviewed.gold_predicate,
                "object": reviewed.gold_object,
                "reviewer_decision": reviewed.reviewer_decision,
                "reviewer_notes": reviewed.reviewer_notes,
            }
        )
    primary = next(
        (
            claim
            for claim in target_claims
            if claim["reviewer_decision"] in {"accept", "revise"}
        ),
        target_claims[0] if target_claims else {},
    )
    updated["gold_claim_json"] = primary
    updated["gold_claims_json"] = target_claims
    return updated


def _best_gold_similarity(row: dict[str, Any], quote_groups: list[ReviewedQuoteGroup]) -> float:
    claim_blob = " ".join(
        str(row.get(key, ""))
        for key in ("selected_claim_text", "selected_subject", "selected_predicate", "selected_object")
    )
    best = 0.0
    for group in quote_groups:
        gold_blob = " ".join([group.source_quote, *group.target_claim_texts])
        best = max(best, _text_similarity(claim_blob, gold_blob))
    return best


def _text_similarity(left: Any, right: Any) -> float:
    left_norm = _normalize_text(str(left or ""))
    right_norm = _normalize_text(str(right or ""))
    if not left_norm or not right_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), 1)
    return (ratio * 0.65) + (overlap * 0.35)


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\s]+", " ", value.lower()).strip()


def _score(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _mean(values: Any) -> float:
    scores = [_score(value) for value in values]
    scores = [score for score in scores if score > 0]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 3)
