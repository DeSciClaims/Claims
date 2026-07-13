from __future__ import annotations

import ast
import json
import re
from typing import Any

from validator.judge_v1.reviewer_export_utils import serialize_export_value

from .dspy_runtime import JudgeV2DSPyRuntime
from .intrinsic_runner import extraction_mode_from_output


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
                    "extraction_mode": row.get("extraction_mode", ""),
                }
            ),
        )
        return normalize_llm_audit_payload(_parse_json_like(getattr(prediction, "audit_json", "")))

    def discover_missing_claims(self, paper_output: dict[str, Any]) -> dict[str, Any]:
        paper_payload = _paper_discovery_payload(paper_output)
        prediction = self.missing_claims_program(
            paper_json=serialize_export_value(paper_payload),
            extracted_claims_json=serialize_export_value(_extracted_claims_payload(paper_output)),
        )
        return normalize_missing_claims_payload(
            _parse_json_like(getattr(prediction, "missing_claims_json", "")),
            allowed_source_span_ids=(
                set(paper_payload.get("allowed_source_span_ids") or [])
                if paper_payload.get("allowed_source_span_ids")
                else None
            ),
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


def normalize_missing_claims_payload(
    payload: dict[str, Any],
    *,
    allowed_source_span_ids: set[str] | None = None,
) -> dict[str, Any]:
    candidates = payload.get("candidate_missing_claims")
    if not isinstance(candidates, list):
        candidates = []
    normalized_candidates: list[dict[str, Any]] = []
    filtered_count = 0
    for item in candidates:
        if not isinstance(item, dict):
            continue
        claim_text = str(item.get("candidate_claim_text", "")).strip()
        if not claim_text:
            continue
        source_span_ids = _string_list(item.get("source_span_ids"))
        if allowed_source_span_ids is not None:
            if not source_span_ids or any(span_id not in allowed_source_span_ids for span_id in source_span_ids):
                filtered_count += 1
                continue
        normalized_candidates.append(
            {
                "candidate_claim_text": claim_text,
                "candidate_subject": str(item.get("candidate_subject", "")).strip(),
                "candidate_predicate": str(item.get("candidate_predicate", "")).strip(),
                "candidate_object": str(item.get("candidate_object", "")).strip(),
                "source_span_ids": source_span_ids,
                "confidence": _score(item.get("confidence")),
                "missing_reason": str(item.get("missing_reason", "")).strip(),
            }
        )
    coverage_comment = str(payload.get("coverage_comment", "")).strip()
    if filtered_count:
        coverage_comment = (
            f"{coverage_comment} Filtered {filtered_count} missing-claim candidates outside the extraction scope."
        ).strip()
    return {
        "candidate_missing_claims": normalized_candidates,
        "coverage_comment": coverage_comment,
    }


def _extracted_claim_payload(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("extractor_metadata_json") if isinstance(row.get("extractor_metadata_json"), dict) else {}
    return {
        "claim_id": row.get("claim_id", ""),
        "claim_profile": row.get("claim_profile", ""),
        "claim_text": row.get("selected_claim_text", ""),
        "source_span_ids": metadata.get("source_span_ids", []),
        "linked_evidence_ids": row.get("linked_evidence_ids", ""),
        "extraction_mode": row.get("extraction_mode", ""),
    }


def _paper_discovery_payload(paper_output: dict[str, Any]) -> dict[str, Any]:
    paper = paper_output.get("paper") if isinstance(paper_output.get("paper"), dict) else {}
    extraction_mode = extraction_mode_from_output(paper_output)
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
    scoped_sections = _coverage_scope_sections(paper_output, sections, extraction_mode)
    allowed_source_span_ids = sorted(
        {
            str(span_id)
            for section in scoped_sections
            if isinstance(section, dict)
            for span_id in section.get("span_ids", [])
            if str(span_id).strip()
        }
    )
    coverage_scope = (
        "abstract_only"
        if extraction_mode == "abstract-full-paper"
        else "full_text_relevant_sections"
    )
    return {
        "extraction_mode": extraction_mode,
        "coverage_scope": coverage_scope,
        "allowed_source_span_ids": allowed_source_span_ids,
        "paper": {
            "paper_id": paper.get("paper_id", ""),
            "title": paper.get("title", ""),
            "doi": paper.get("doi", ""),
            "year": paper.get("year", ""),
        },
        "paper_summary": _coverage_scope_paper_summary(paper_output, extraction_mode),
        "sections": [
            _section_discovery_payload(section, summary_by_section, plan_by_section)
            for section in scoped_sections
            if isinstance(section, dict)
        ],
    }


def _coverage_scope_sections(
    paper_output: dict[str, Any],
    sections: list[Any],
    extraction_mode: str,
) -> list[dict[str, Any]]:
    dict_sections = [section for section in sections if isinstance(section, dict)]
    if extraction_mode != "abstract-full-paper":
        return dict_sections

    abstract_section_id = ""
    abstract_block = paper_output.get("abstract_claim_extraction")
    if isinstance(abstract_block, dict):
        abstract_section_id = str(abstract_block.get("abstract_section_id", "") or "")
        abstract_section = abstract_block.get("abstract_section")
        if isinstance(abstract_section, dict):
            return [abstract_section]

    scoped = [
        section
        for section in dict_sections
        if str(section.get("section_id", "") or "") == abstract_section_id
        or _is_abstract_section(section)
    ]
    return scoped[:1]


def _coverage_scope_paper_summary(paper_output: dict[str, Any], extraction_mode: str) -> dict[str, Any]:
    if extraction_mode != "abstract-full-paper":
        summary = paper_output.get("paper_summary")
        return summary if isinstance(summary, dict) else {}
    return {
        "coverage_scope": "abstract_only",
        "instruction": (
            "Coverage diagnostics for abstract-full-paper mode must identify only claims made in the abstract. "
            "Full-paper body claims are out of scope for missing-claim coverage."
        ),
    }


def _is_abstract_section(section: dict[str, Any]) -> bool:
    section_type = str(section.get("section_type", "") or "").strip().lower()
    section_name = str(section.get("section_name", "") or "").strip().lower()
    original_name = str(section.get("original_section_name", "") or "").strip().lower()
    return section_type == "abstract" or section_name == "abstract" or original_name == "abstract"


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
            "claim_profile": claim.get("claim_profile", ""),
            "source_span_ids": claim.get("source_span_ids", []),
        }
        for claim in claims
        if isinstance(claim, dict)
    ]

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
