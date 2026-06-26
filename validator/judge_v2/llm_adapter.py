from __future__ import annotations

import ast
import json
import re
from typing import Any

from validator.judge_v1.reviewer_export_utils import serialize_export_value

from .dspy_runtime import JudgeV2DSPyRuntime


class JudgeV2LLMAdapter:
    def __init__(self, *, runtime: JudgeV2DSPyRuntime) -> None:
        self.program = runtime.audit_program

    def audit_row(self, row: dict[str, Any], *, audit_mode: str) -> dict[str, Any]:
        prediction = self.program(
            audit_mode=audit_mode,
            source_quote=str(row.get("source_quote", "")),
            extracted_claim_json=serialize_export_value(_extracted_claim_payload(row)),
            evidence_items_json=serialize_export_value(row.get("group_evidence_items_json", [])),
            claim_evidence_links_json=serialize_export_value(row.get("group_links_json", [])),
            gold_claim_json=serialize_export_value(row.get("gold_claim_json", {})),
            paper_context_json=serialize_export_value(
                {
                    "section_summary": row.get("section_summary_json", {}),
                    "paper_summary": row.get("paper_summary_json", {}),
                    "paper_claim_registry": row.get("paper_claim_registry_json", []),
                    "paper_evidence_registry": row.get("paper_evidence_registry_json", []),
                }
            ),
        )
        return normalize_llm_audit_payload(_parse_json_like(getattr(prediction, "audit_json", "")))


def normalize_llm_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "audit_status": _string_choice(payload.get("audit_status"), {"accepted", "needs_correction", "rejected", "uncertain"}, "uncertain"),
        "overall_score": _score(payload.get("overall_score")),
        "complete_coverage_score": _score(payload.get("complete_coverage_score")),
        "complete_coverage_comment": str(payload.get("complete_coverage_comment", "")),
        "accurate_extraction_score": _score(payload.get("accurate_extraction_score")),
        "accurate_extraction_comment": str(payload.get("accurate_extraction_comment", "")),
        "evidence_evaluation_score": _score(payload.get("evidence_evaluation_score")),
        "evidence_evaluation_comment": str(payload.get("evidence_evaluation_comment", "")),
        "primary_issue": str(payload.get("primary_issue", "")),
        "issue_tags": _string_list(payload.get("issue_tags")),
        "missing_elements": _string_list(payload.get("missing_elements")),
        "suggested_corrections_json": payload.get("suggested_corrections_json") if isinstance(payload.get("suggested_corrections_json"), dict) else {},
        "comments": str(payload.get("comments", "")),
    }
    scores = [
        normalized["complete_coverage_score"],
        normalized["accurate_extraction_score"],
        normalized["evidence_evaluation_score"],
    ]
    if normalized["overall_score"] is None:
        normalized["overall_score"] = round(sum(scores) / len(scores), 3)
    return normalized


def _extracted_claim_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": row.get("claim_id", ""),
        "claim_profile": row.get("claim_profile", ""),
        "claim_text": row.get("selected_claim_text", ""),
        "subject": row.get("selected_subject", ""),
        "predicate": row.get("selected_predicate", ""),
        "object": row.get("selected_object", ""),
        "context": row.get("extracted_context_json", {}),
        "details": row.get("extracted_details_json", {}),
        "linked_evidence_ids": row.get("linked_evidence_ids", ""),
    }


def _parse_json_like(raw_output: str) -> dict[str, Any]:
    stripped = str(raw_output or "").strip()
    candidates = [stripped]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _score(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    return round(min(1.0, max(0.0, number)), 3)


def _string_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
