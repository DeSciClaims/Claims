from __future__ import annotations

import ast
import json
import re
from typing import Any


DIAGNOSTIC_KEYS = (
    "claim_text_ok",
    "scope_preserved",
    "atomicity_ok",
    "subject_ok",
    "predicate_ok",
    "object_ok",
    "meta_claim_problem",
    "over_split_problem",
    "under_split_problem",
)

SUBSCORE_KEYS = (
    "claim_text_quality",
    "scope_preservation",
    "atomicity",
    "subject_quality",
    "predicate_quality",
    "object_quality",
)


def parse_judge_payload(raw_output: str) -> dict[str, Any]:
    candidates = _json_candidates(raw_output)
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
    return default_judge_payload(raw_output)


def default_judge_payload(raw_output: str = "") -> dict[str, Any]:
    return {
        "decision": "parse_error",
        "overall_score": "",
        "error_tags": [],
        "feedback": f"Could not parse judge output: {str(raw_output or '')[:300]}",
        "diagnostics": {key: None for key in DIAGNOSTIC_KEYS},
        "subscores": {key: "" for key in SUBSCORE_KEYS},
        "primary_failure": "",
    }


def flatten_judge_payload(parsed: dict[str, Any]) -> dict[str, str]:
    diagnostics = parsed.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    subscores = parsed.get("subscores", {})
    if not isinstance(subscores, dict):
        subscores = {}
    flat = {
        "llm_judge_decision": str(parsed.get("decision", "parse_error")).strip().lower() or "parse_error",
        "llm_judge_overall_score": str(parsed.get("overall_score", "")),
        "llm_judge_error_tags": ", ".join(str(tag) for tag in parsed.get("error_tags", [])),
        "llm_judge_feedback": str(parsed.get("feedback", "")),
        "llm_judge_primary_failure": str(parsed.get("primary_failure", "")),
    }
    for key in DIAGNOSTIC_KEYS:
        value = diagnostics.get(key)
        flat[f"llm_judge_{key}"] = "" if value is None else str(bool(value)).lower()
    for key in SUBSCORE_KEYS:
        flat[f"llm_judge_{key}"] = str(subscores.get(key, ""))
    return flat


def _json_candidates(raw_output: str) -> list[str]:
    stripped = str(raw_output or "").strip()
    candidates = [stripped]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    object_start = stripped.find("{")
    object_end = stripped.rfind("}")
    if object_start != -1 and object_end != -1 and object_end > object_start:
        sliced = stripped[object_start : object_end + 1].strip()
        if sliced not in candidates:
            candidates.append(sliced)
    return candidates
