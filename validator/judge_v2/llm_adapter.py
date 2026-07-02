from __future__ import annotations

import ast
import json
import re
from typing import Any

from validator.judge_v1.reviewer_export_utils import serialize_export_value

from .dspy_runtime import JudgeV2DSPyRuntime


class JudgeV2LLMAdapter:
    def __init__(self, *, runtime: JudgeV2DSPyRuntime) -> None:
        self.audit_program = runtime.audit_program
        self.missing_claims_program = runtime.missing_claims_program

    def audit_row(self, row: dict[str, Any], *, audit_mode: str) -> dict[str, Any]:
        prediction = self.audit_program(
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

    def discover_missing_claims(self, paper_output: dict[str, Any]) -> dict[str, Any]:
        prediction = self.missing_claims_program(
            paper_json=serialize_export_value(_paper_discovery_payload(paper_output)),
            extracted_claims_json=serialize_export_value(_extracted_claims_payload(paper_output)),
        )
        return normalize_missing_claims_payload(
            _parse_json_like(getattr(prediction, "missing_claims_json", ""))
        )


def normalize_llm_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "audit_status": _string_choice(payload.get("audit_status"), {"accepted", "needs_correction", "rejected", "uncertain"}, "uncertain"),
        "overall_score": _score(payload.get("overall_score")),
        "complete_coverage_score": None,
        "complete_coverage_comment": "",
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
        normalized["accurate_extraction_score"],
        normalized["evidence_evaluation_score"],
    ]
    if normalized["overall_score"] is None:
        normalized["overall_score"] = round(sum(scores) / len(scores), 3)
    return normalized


def normalize_missing_claims_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = payload.get("candidate_missing_claims")
    if not isinstance(candidates, list):
        candidates = []
    normalized_candidates: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        claim_text = str(item.get("candidate_claim_text", "")).strip()
        if not claim_text:
            continue
        normalized_candidates.append(
            {
                "candidate_claim_text": claim_text,
                "candidate_subject": str(item.get("candidate_subject", "")).strip(),
                "candidate_predicate": str(item.get("candidate_predicate", "")).strip(),
                "candidate_object": str(item.get("candidate_object", "")).strip(),
                "source_span_ids": _string_list(item.get("source_span_ids")),
                "confidence": _score(item.get("confidence")),
                "missing_reason": str(item.get("missing_reason", "")).strip(),
            }
        )
    return {
        "candidate_missing_claims": normalized_candidates,
        "coverage_comment": str(payload.get("coverage_comment", "")).strip(),
    }


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


def _paper_discovery_payload(paper_output: dict[str, Any]) -> dict[str, Any]:
    paper = paper_output.get("paper") if isinstance(paper_output.get("paper"), dict) else {}
    sections = paper_output.get("sections") if isinstance(paper_output.get("sections"), list) else []
    section_summaries = paper_output.get("section_summaries") if isinstance(paper_output.get("section_summaries"), list) else []
    extraction_plan = paper_output.get("section_extraction_plan") if isinstance(paper_output.get("section_extraction_plan"), list) else []
    summary_by_section = {
        str(item.get("section_id", "")): item
        for item in section_summaries
        if isinstance(item, dict)
    }
    plan_by_section = {
        str(item.get("section_id", "")): item
        for item in extraction_plan
        if isinstance(item, dict)
    }
    return {
        "paper": {
            "paper_id": paper.get("paper_id", ""),
            "title": paper.get("title", ""),
            "doi": paper.get("doi", ""),
            "year": paper.get("year", ""),
        },
        "paper_summary": paper_output.get("paper_summary", {}),
        "sections": [
            _section_discovery_payload(section, summary_by_section, plan_by_section)
            for section in sections
            if isinstance(section, dict)
        ],
    }


def _section_discovery_payload(
    section: dict[str, Any],
    summary_by_section: dict[str, dict[str, Any]],
    plan_by_section: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    section_id = str(section.get("section_id", ""))
    return {
        "section_id": section_id,
        "section_name": section.get("section_name", ""),
        "section_type": section.get("section_type", ""),
        "span_ids": section.get("span_ids", []),
        "text": _truncate_text(str(section.get("text", "")), 6000),
        "summary": summary_by_section.get(section_id, {}),
        "extraction_plan": plan_by_section.get(section_id, {}),
    }


def _extracted_claims_payload(paper_output: dict[str, Any]) -> list[dict[str, Any]]:
    claims = paper_output.get("claims") if isinstance(paper_output.get("claims"), list) else []
    return [
        {
            "claim_id": claim.get("claim_id", ""),
            "claim_text": claim.get("claim_text", ""),
            "subject": _value_field(claim.get("subject")),
            "predicate": _value_field(claim.get("predicate")),
            "object": _value_field(claim.get("object")),
            "claim_profile": claim.get("claim_profile", ""),
            "source_span_ids": claim.get("source_span_ids", []),
        }
        for claim in claims
        if isinstance(claim, dict)
    ]


def _value_field(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value", "") or "")
    return str(value or "")


def _truncate_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


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
