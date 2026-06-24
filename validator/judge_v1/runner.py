from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from .config import JudgeV1Config
from .dspy_runtime import JudgeV1DSPyRuntime
from .export import append_judge_fields, write_evaluation_rows, write_manifest
from .judge_adapter import JudgeAdapter
from .models import EvaluatedClaimMatch
from .review_data import ReviewedQuoteGroup, group_rows_by_quote, load_reviewed_claim_rows_from_file
from .section_inventory import normalize_text


class JudgeV1Runner:
    def __init__(
        self,
        config: JudgeV1Config,
        *,
        runtime: JudgeV1DSPyRuntime | None = None,
    ) -> None:
        self.config = config
        self._runtime = runtime

    def judge_extraction_output_json(
        self,
        *,
        extraction_output_json_path: Path,
        mode: str = "gold",
        gold_reviewed_file: Path | None = None,
        output_dir: Path | None = None,
        judge_version: str = "v1",
        xlsx: bool = False,
    ) -> dict[str, Any]:
        paper_output = json.loads(extraction_output_json_path.read_text(encoding="utf-8"))
        paper_id = str((paper_output.get("paper") or {}).get("paper_id", "")).strip()
        final_output_dir = output_dir or (extraction_output_json_path.parent / f"judge_{mode}_{judge_version}")

        if mode == "intrinsic":
            rows = build_intrinsic_claim_rows(paper_output)
            return self.judge_intrinsic_rows(
                rows=rows,
                output_dir=final_output_dir,
                judge_version=judge_version,
                xlsx=xlsx,
                manifest_extra={
                    "mode": mode,
                    "extraction_output_json_path": str(extraction_output_json_path),
                },
            )

        if gold_reviewed_file is None:
            raise SystemExit("`--gold-reviewed-file` is required when `--mode gold` is used.")

        quote_groups = group_rows_by_quote(load_reviewed_claim_rows_from_file(gold_reviewed_file))
        if paper_id:
            quote_groups = [group for group in quote_groups if group.paper_id == paper_id]
        if not quote_groups:
            raise SystemExit(
                f"No reviewed quote groups found for paper_id `{paper_id}` in {gold_reviewed_file}"
            )
        return self.judge_paper_outputs(
            paper_outputs={paper_id: paper_output} if paper_id else {},
            quote_groups=quote_groups,
            reviewed_file=gold_reviewed_file,
            output_dir=final_output_dir,
            judge_version=judge_version,
            xlsx=xlsx,
            manifest_extra={
                "mode": mode,
                "extraction_output_json_path": str(extraction_output_json_path),
                "gold_reviewed_file": str(gold_reviewed_file),
            },
        )

    def judge_paper_outputs(
        self,
        *,
        paper_outputs: dict[str, dict[str, Any]],
        quote_groups: list[ReviewedQuoteGroup],
        reviewed_file: Path,
        output_dir: Path,
        judge_version: str = "v1",
        xlsx: bool = False,
        manifest_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evaluation_rows = [match_group_to_claim(group, paper_outputs.get(group.paper_id)) for group in quote_groups]
        evaluation_rows = self._append_judge_fields_if_requested(rows=evaluation_rows, judge_version=judge_version)
        evaluation_path = output_dir / (
            "section_context_v1_gold_evaluation.xlsx" if xlsx else "section_context_v1_gold_evaluation.csv"
        )
        write_evaluation_rows(evaluation_path, evaluation_rows, xlsx=xlsx, mode="gold")
        manifest = {
            "output_dir": str(output_dir),
            "reviewed_file": str(reviewed_file),
            "judge_version": judge_version,
            "judge_mode": "gold",
            "paper_count": len(paper_outputs),
            "group_count": len(quote_groups),
            "evaluated_paper_ids": sorted(paper_outputs.keys()),
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        write_manifest(output_dir / "manifest.json", manifest)
        return {"paper_outputs": paper_outputs, "evaluation_rows": evaluation_rows}

    def judge_intrinsic_rows(
        self,
        *,
        rows: list[dict[str, Any]],
        output_dir: Path,
        judge_version: str,
        xlsx: bool,
        manifest_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        judged_rows = self._append_judge_fields_if_requested(rows=rows, judge_version=judge_version)
        evaluation_path = output_dir / (
            "section_context_v1_intrinsic_judgment.xlsx" if xlsx else "section_context_v1_intrinsic_judgment.csv"
        )
        write_evaluation_rows(evaluation_path, judged_rows, xlsx=xlsx, mode="intrinsic")
        manifest = {
            "output_dir": str(output_dir),
            "judge_version": judge_version,
            "judge_mode": "intrinsic",
            "group_count": len(rows),
            "claim_count": sum(1 for row in rows if row.get("claim_id")),
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        write_manifest(output_dir / "manifest.json", manifest)
        return {"evaluation_rows": judged_rows}

    def _get_runtime(self) -> JudgeV1DSPyRuntime:
        if self._runtime is None:
            self._runtime = self.config.create_runtime()
        return self._runtime

    def _append_judge_fields_if_requested(
        self,
        *,
        rows: list[dict[str, Any]],
        judge_version: str,
    ) -> list[dict[str, Any]]:
        if judge_version == "none":
            return rows
        judge = JudgeAdapter(runtime=self._get_runtime(), judge_version=judge_version)
        judged = [judge.judge_row(row) for row in rows]
        return append_judge_fields(rows, judged)


def match_group_to_claim(group: ReviewedQuoteGroup, paper_output: dict[str, Any] | None) -> dict[str, Any]:
    if not paper_output:
        return EvaluatedClaimMatch(
            paper_id=group.paper_id,
            group_id=group.group_id,
            section_title=group.section_title,
            source_quote=group.source_quote,
        ).model_dump(mode="json")
    sections = paper_output.get("sections", [])
    claims = paper_output.get("claims", [])
    evidence_items = paper_output.get("evidence_items", [])
    links = paper_output.get("claim_evidence_links", [])
    section_id = _match_section_id(group.section_title, sections, group.source_quote)
    candidate_claims = [claim for claim in claims if _claim_matches_section(claim, section_id, sections)]
    if not candidate_claims:
        candidate_claims = claims
    best_row: dict[str, Any] | None = None
    best_score = -1.0
    for claim in candidate_claims:
        claim_id = str(claim.get("claim_id", ""))
        claim_links = [link for link in links if str(link.get("claim_id", "")) == claim_id]
        linked_ids = [
            str(link.get("evidence_id", ""))
            for link in claim_links
            if str(link.get("evidence_id", "")).strip()
        ]
        claim_evidence = [item for item in evidence_items if str(item.get("evidence_id", "")) in linked_ids]
        score = _match_score(group.source_quote, claim, claim_evidence)
        if score > best_score:
            best_score = score
            best_row = {
                "paper_id": group.paper_id,
                "group_id": group.group_id,
                "section_title": group.section_title,
                "source_quote": group.source_quote,
                "matched_section_id": section_id,
                "matched_section_name": _section_name_for_id(section_id, sections),
                "match_score": round(score, 4),
                "claim_id": claim_id,
                "claim_profile": str(claim.get("claim_profile", "")),
                "selected_claim_text": str(claim.get("claim_text", "")),
                "selected_subject": _semantic_value(claim.get("subject")),
                "selected_predicate": _semantic_value(claim.get("predicate")),
                "selected_object": _semantic_value(claim.get("object")),
                "extracted_context_json": claim.get("context", {}),
                "extracted_details_json": claim.get("details", {}),
                "extractor_metadata_json": {
                    "section_context_pipeline": "section_context_v1",
                    "source_span_ids": claim.get("source_span_ids", []),
                },
                "linked_evidence_ids": "; ".join(linked_ids),
                "group_evidence_items_json": claim_evidence,
                "group_links_json": claim_links,
            }
    if best_row is None:
        return EvaluatedClaimMatch(
            paper_id=group.paper_id,
            group_id=group.group_id,
            section_title=group.section_title,
            source_quote=group.source_quote,
            matched_section_id=section_id,
            matched_section_name=_section_name_for_id(section_id, sections),
        ).model_dump(mode="json")
    best_row.update(
        _build_paper_context_row_fields(
            paper_output=paper_output,
            current_claim=next(
                (
                    claim
                    for claim in claims
                    if str(claim.get("claim_id", "")) == str(best_row.get("claim_id", ""))
                ),
                None,
            ),
            section_id=section_id,
        )
    )
    return best_row


def _match_section_id(section_title: str, sections: list[dict[str, Any]], source_quote: str) -> str | None:
    normalized_title = normalize_text(section_title).lower()
    best_id = None
    best_score = -1.0
    for section in sections:
        candidate_title = normalize_text(section.get("section_name", "")).lower()
        ratio = difflib.SequenceMatcher(None, normalized_title, candidate_title).ratio()
        if normalize_text(source_quote) and normalize_text(source_quote)[:160].lower() in normalize_text(
            section.get("text", "")
        ).lower():
            ratio += 0.25
        if ratio > best_score:
            best_id = section.get("section_id")
            best_score = ratio
    return str(best_id) if best_id else None


def _match_score(source_quote: str, claim: dict[str, Any], evidence_items: list[dict[str, Any]]) -> float:
    claim_text = str(claim.get("claim_text", ""))
    evidence_blob = " ".join(str(item.get("summary_text", "")) for item in evidence_items)
    source_norm = _normalize_match_text(source_quote)
    claim_norm = _normalize_match_text(claim_text)
    evidence_norm = _normalize_match_text(evidence_blob)
    claim_ratio = difflib.SequenceMatcher(None, source_norm, claim_norm).ratio()
    evidence_ratio = difflib.SequenceMatcher(None, source_norm, evidence_norm).ratio()
    token_overlap = _token_overlap(source_norm, claim_norm + " " + evidence_norm)
    return (0.45 * claim_ratio) + (0.35 * evidence_ratio) + (0.20 * token_overlap)


def _normalize_match_text(text: str) -> str:
    normalized = normalize_text(text).lower()
    return re.sub(r"[^a-z0-9\s]+", " ", normalized)


def _token_overlap(left: str, right: str) -> float:
    left_tokens = {token for token in left.split() if token}
    right_tokens = {token for token in right.split() if token}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), 1)


def _claim_matches_section(claim: dict[str, Any], section_id: str | None, sections: list[dict[str, Any]]) -> bool:
    if not section_id:
        return False
    span_ids = set(claim.get("source_span_ids", []) or [])
    for section in sections:
        if str(section.get("section_id")) != str(section_id):
            continue
        return bool(span_ids & set(section.get("span_ids", []) or []))
    return False


def _section_name_for_id(section_id: str | None, sections: list[dict[str, Any]]) -> str | None:
    for section in sections:
        if str(section.get("section_id")) == str(section_id):
            return str(section.get("section_name", ""))
    return None


def _semantic_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return str(value or "")


def build_intrinsic_claim_rows(paper_output: dict[str, Any]) -> list[dict[str, Any]]:
    paper_id = str((paper_output.get("paper") or {}).get("paper_id", "")).strip()
    sections = paper_output.get("sections", [])
    claims = paper_output.get("claims", [])
    evidence_items = paper_output.get("evidence_items", [])
    links = paper_output.get("claim_evidence_links", [])

    section_by_id = {
        str(section.get("section_id", "")): section
        for section in sections
        if str(section.get("section_id", "")).strip()
    }
    section_by_span_id: dict[str, dict[str, Any]] = {}
    for section in sections:
        for span_id in section.get("span_ids", []) or []:
            span_key = str(span_id).strip()
            if span_key and span_key not in section_by_span_id:
                section_by_span_id[span_key] = section

    evidence_by_id = {
        str(item.get("evidence_id", "")): item
        for item in evidence_items
        if str(item.get("evidence_id", "")).strip()
    }

    rows: list[dict[str, Any]] = []
    for index, claim in enumerate(claims, start=1):
        claim_id = str(claim.get("claim_id", "")).strip()
        source_span_ids = [
            str(value).strip()
            for value in claim.get("source_span_ids", []) or []
            if str(value).strip()
        ]
        section = _match_intrinsic_section_for_claim(
            claim=claim,
            section_by_id=section_by_id,
            section_by_span_id=section_by_span_id,
        )
        claim_links = [link for link in links if str(link.get("claim_id", "")).strip() == claim_id]
        linked_ids = [
            str(link.get("evidence_id", "")).strip()
            for link in claim_links
            if str(link.get("evidence_id", "")).strip()
        ]
        claim_evidence = [evidence_by_id[evidence_id] for evidence_id in linked_ids if evidence_id in evidence_by_id]
        section_id = str(section.get("section_id", "")).strip() if section else None
        section_name = str(section.get("section_name", "")).strip() if section else ""
        section_text = str(section.get("text", "")).strip() if section else ""
        rows.append(
            {
                "paper_id": paper_id,
                "group_id": claim_id or f"intrinsic_claim_{index:04d}",
                "section_title": section_name,
                "source_quote": section_text,
                "matched_section_id": section_id,
                "matched_section_name": section_name or None,
                "match_score": 1.0 if section_text else 0.0,
                "claim_id": claim_id,
                "claim_profile": str(claim.get("claim_profile", "")),
                "selected_claim_text": str(claim.get("claim_text", "")),
                "selected_subject": _semantic_value(claim.get("subject")),
                "selected_predicate": _semantic_value(claim.get("predicate")),
                "selected_object": _semantic_value(claim.get("object")),
                "extracted_context_json": claim.get("context", {}) or {},
                "extracted_details_json": claim.get("details", {}) or {},
                "extractor_metadata_json": {
                    "section_context_pipeline": "section_context_v1",
                    "judge_mode": "intrinsic",
                    "source_span_ids": source_span_ids,
                    "section_id": section_id,
                },
                "linked_evidence_ids": "; ".join(linked_ids),
                "group_evidence_items_json": claim_evidence,
                "group_links_json": claim_links,
                **_build_paper_context_row_fields(
                    paper_output=paper_output,
                    current_claim=claim,
                    section_id=section_id,
                ),
            }
        )
    return rows


def _match_intrinsic_section_for_claim(
    *,
    claim: dict[str, Any],
    section_by_id: dict[str, dict[str, Any]],
    section_by_span_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    context = claim.get("context")
    if isinstance(context, dict):
        related_section = context.get("related_section")
        if isinstance(related_section, dict):
            related_value = str(related_section.get("value", "")).strip()
        else:
            related_value = str(related_section or "").strip()
        if related_value and related_value in section_by_id:
            return section_by_id[related_value]

    for span_id in claim.get("source_span_ids", []) or []:
        span_key = str(span_id).strip()
        if span_key and span_key in section_by_span_id:
            return section_by_span_id[span_key]
    return None


def _build_paper_context_row_fields(
    *,
    paper_output: dict[str, Any],
    current_claim: dict[str, Any] | None,
    section_id: str | None,
) -> dict[str, Any]:
    section_summary_by_id = {
        str(item.get("section_id", "")).strip(): item
        for item in paper_output.get("section_summaries", []) or []
        if str(item.get("section_id", "")).strip()
    }
    sections = paper_output.get("sections", []) or []
    evidence_items = paper_output.get("evidence_items", []) or []
    links = paper_output.get("claim_evidence_links", []) or []
    claims = paper_output.get("claims", []) or []
    section_name_by_span_id = _section_name_by_span_id(sections)
    linked_ids_by_claim = _linked_ids_by_claim(links)
    current_claim_id = str((current_claim or {}).get("claim_id", "")).strip()
    return {
        "section_summary_json": section_summary_by_id.get(str(section_id or "").strip(), {}),
        "paper_summary_json": paper_output.get("paper_summary", {}) or {},
        "paper_claim_registry_json": [
            _simplify_claim_for_registry(
                claim=claim,
                linked_ids=linked_ids_by_claim.get(str(claim.get("claim_id", "")).strip(), []),
                section_name=_section_name_for_claim(claim, sections, section_name_by_span_id),
            )
            for claim in claims
            if str(claim.get("claim_id", "")).strip()
            and str(claim.get("claim_id", "")).strip() != current_claim_id
        ],
        "paper_evidence_registry_json": [
            _simplify_evidence_for_registry(item, section_name_by_span_id)
            for item in evidence_items
        ],
    }


def _section_name_by_span_id(sections: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for section in sections:
        section_name = str(section.get("section_name", "")).strip()
        for span_id in section.get("span_ids", []) or []:
            span_key = str(span_id).strip()
            if span_key and span_key not in mapping:
                mapping[span_key] = section_name
    return mapping


def _linked_ids_by_claim(links: list[dict[str, Any]]) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for link in links:
        claim_id = str(link.get("claim_id", "")).strip()
        evidence_id = str(link.get("evidence_id", "")).strip()
        if not claim_id or not evidence_id:
            continue
        output.setdefault(claim_id, []).append(evidence_id)
    return output


def _section_name_for_claim(
    claim: dict[str, Any],
    sections: list[dict[str, Any]],
    section_name_by_span_id: dict[str, str],
) -> str:
    matched = _match_intrinsic_section_for_claim(
        claim=claim,
        section_by_id={
            str(section.get("section_id", "")).strip(): section
            for section in sections
            if str(section.get("section_id", "")).strip()
        },
        section_by_span_id={
            span_id: next(
                (
                    section
                    for section in sections
                    if span_id in {str(value).strip() for value in section.get("span_ids", []) or []}
                ),
                {},
            )
            for span_id in section_name_by_span_id
        },
    )
    if matched:
        return str(matched.get("section_name", "")).strip()
    for span_id in claim.get("source_span_ids", []) or []:
        name = section_name_by_span_id.get(str(span_id).strip(), "")
        if name:
            return name
    return ""


def _simplify_claim_for_registry(
    *,
    claim: dict[str, Any],
    linked_ids: list[str],
    section_name: str,
) -> dict[str, Any]:
    return {
        "claim_id": str(claim.get("claim_id", "")).strip(),
        "claim_profile": str(claim.get("claim_profile", "")).strip(),
        "claim_text": str(claim.get("claim_text", "")).strip(),
        "subject": _semantic_value(claim.get("subject")),
        "predicate": _semantic_value(claim.get("predicate")),
        "object": _semantic_value(claim.get("object")),
        "section_name": section_name,
        "linked_evidence_ids": linked_ids,
    }


def _simplify_evidence_for_registry(item: dict[str, Any], section_name_by_span_id: dict[str, str]) -> dict[str, Any]:
    source_span_ids = [
        str(value).strip()
        for value in item.get("source_span_ids", []) or []
        if str(value).strip()
    ]
    section_name = ""
    for span_id in source_span_ids:
        if span_id in section_name_by_span_id:
            section_name = section_name_by_span_id[span_id]
            break
    evidence_method = item.get("evidence_method")
    if isinstance(evidence_method, dict):
        evidence_method_value = str(evidence_method.get("value", "")).strip()
    else:
        evidence_method_value = str(evidence_method or "").strip()
    return {
        "evidence_id": str(item.get("evidence_id", "")).strip(),
        "summary_text": str(item.get("summary_text", "")).strip(),
        "evidence_method": evidence_method_value,
        "section_name": section_name,
    }
