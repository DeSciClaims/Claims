from __future__ import annotations

import ast
import json
from typing import Any

from .reviewer_export_utils import serialize_export_value
from .rubric import flatten_judge_v1_payload

from .dspy_runtime import JudgeV1DSPyRuntime


class JudgeAdapter:
    def __init__(
        self,
        *,
        runtime: JudgeV1DSPyRuntime,
        judge_version: str,
    ) -> None:
        self.judge_version = judge_version
        self.program = runtime.get_judge_program(judge_version=judge_version)

    def judge_row(self, row: dict[str, Any]) -> dict[str, str]:
        if self.program is None:
            return {}
        prediction = self.program(
            section_title=str(row.get("section_title", "")),
            source_quote=str(row.get("source_quote", "")),
            extracted_claim_text=str(row.get("selected_claim_text", "")),
            extracted_subject=str(row.get("selected_subject", "")),
            extracted_predicate=str(row.get("selected_predicate", "")),
            extracted_object=str(row.get("selected_object", "")),
            extracted_context_json=serialize_export_value(row.get("extracted_context_json", {})),
            extracted_details_json=serialize_export_value(row.get("extracted_details_json", {})),
            group_evidence_items_json=serialize_export_value(row.get("group_evidence_items_json", [])),
            group_links_json=serialize_export_value(row.get("group_links_json", [])),
            linked_evidence_ids=str(row.get("linked_evidence_ids", "")),
            section_summary_json=serialize_export_value(row.get("section_summary_json", {})),
            paper_summary_json=serialize_export_value(row.get("paper_summary_json", {})),
            paper_claim_registry_json=serialize_export_value(row.get("paper_claim_registry_json", [])),
            paper_evidence_registry_json=serialize_export_value(row.get("paper_evidence_registry_json", [])),
        )
        parsed = _parse_json_like(getattr(prediction, "judge_json", ""))
        return flatten_judge_v1_payload(parsed)


def _parse_json_like(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_output)
    except Exception:
        try:
            parsed = ast.literal_eval(raw_output)
        except Exception:
            parsed = {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
