from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .reviewer_export_utils import (
    linked_evidence_ids_for_claim,
    normalize_extractor_metadata_payload,
    serialize_export_value,
    summarize_context,
    summarize_details,
    summarize_evidence_items,
    write_reviewer_rows,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import EvaluatedClaimMatch


class Claim:  # pragma: no cover - typing helper for exported rows
    claim_id: str
    paper_id: str
    claim_text: str
    claim_profile: str | None
    subject: object
    predicate: object
    object: object
    context: dict[str, object]
    details: dict[str, object]


class ClaimEvidenceLink:  # pragma: no cover - typing helper for exported rows
    claim_id: str
    evidence_id: str


class EvidenceItem:  # pragma: no cover - typing helper for exported rows
    evidence_id: str


EXTRACTION_ROW_FIELDS = [
    "paper_id",
    "section_id",
    "section_name",
    "section_type",
    "claim_id",
    "claim_profile",
    "claim_text",
    "subject",
    "predicate",
    "object",
    "context_summary",
    "context_json",
    "details_summary",
    "details_json",
    "linked_evidence_ids",
    "evidence_count",
    "evidence_summary",
    "evidence_items_json",
    "links_json",
]

INTRINSIC_EVALUATION_ROW_FIELDS = [
    "paper_id",
    "section_id",
    "section_name",
    "section_text",
    "claim_id",
    "claim_profile",
    "claim_text",
    "subject",
    "predicate",
    "object",
    "context_summary",
    "context_json",
    "details_summary",
    "details_json",
    "extractor_metadata_summary",
    "extractor_metadata_json",
    "section_summary_json",
    "paper_summary_json",
    "paper_claim_registry_json",
    "paper_evidence_registry_json",
    "linked_evidence_ids",
    "evidence_summary",
    "evidence_items_json",
    "claim_evidence_links_json",
]

GOLD_EVALUATION_ROW_FIELDS = [
    "paper_id",
    "review_group_id",
    "review_section_name",
    "review_source_quote",
    "section_id",
    "section_name",
    "match_score",
    "claim_id",
    "claim_profile",
    "claim_text",
    "subject",
    "predicate",
    "object",
    "context_summary",
    "context_json",
    "details_summary",
    "details_json",
    "extractor_metadata_summary",
    "extractor_metadata_json",
    "section_summary_json",
    "paper_summary_json",
    "paper_claim_registry_json",
    "paper_evidence_registry_json",
    "linked_evidence_ids",
    "evidence_summary",
    "evidence_items_json",
    "claim_evidence_links_json",
]


def write_json(output_path: Path, payload: Any) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_manifest(output_path: Path, payload: Any) -> None:
    write_json(output_path, payload)


def write_extraction_rows(
    output_path: Path,
    *,
    claims: list[Claim],
    evidence_items: list[EvidenceItem],
    links: list[ClaimEvidenceLink],
    xlsx: bool,
) -> None:
    claim_links = [link.model_dump(mode="json") for link in links]
    evidence_by_id = {item.evidence_id: item for item in evidence_items}
    rows: list[dict[str, Any]] = []
    for claim in claims:
        linked_ids = linked_evidence_ids_for_claim(claim.claim_id, claim_links)
        group_evidence_items = [
            evidence_by_id[link.evidence_id].model_dump(mode="json")
            for link in links
            if link.claim_id == claim.claim_id and link.evidence_id in evidence_by_id
        ]
        row = {
            "paper_id": claim.paper_id,
            "section_id": claim.source_span_ids[0] if claim.source_span_ids else "",
            "section_name": "",
            "section_type": "",
            "claim_id": claim.claim_id,
            "claim_profile": getattr(claim, "claim_profile", "") or "",
            "claim_text": claim.claim_text,
            "subject": claim.subject.value,
            "predicate": claim.predicate.value,
            "object": claim.object.value,
            "context_summary": summarize_context(claim.context),
            "context_json": serialize_export_value(claim.context),
            "details_summary": summarize_details(claim.details),
            "details_json": serialize_export_value(claim.details),
            "linked_evidence_ids": linked_ids,
            "evidence_count": len(group_evidence_items),
            "evidence_summary": summarize_evidence_items(group_evidence_items),
            "evidence_items_json": serialize_export_value(group_evidence_items),
            "links_json": serialize_export_value(
                [link.model_dump(mode="json") for link in links if link.claim_id == claim.claim_id]
            ),
        }
        rows.append(row)
    write_reviewer_rows(rows, output_path, EXTRACTION_ROW_FIELDS, xlsx=xlsx)


def write_evaluation_rows(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    xlsx: bool,
    mode: str,
) -> None:
    output_rows = []
    for row in rows:
        extractor_metadata_json = row.get("extractor_metadata_json", {})
        judge_fields = {
            key: value
            for key, value in row.items()
            if key.startswith("llm_judge_")
        }
        common = {
            "paper_id": row.get("paper_id", ""),
            "section_id": row.get("matched_section_id", ""),
            "section_name": row.get("matched_section_name", "") or row.get("section_title", ""),
            "claim_id": row.get("claim_id", ""),
            "claim_profile": row.get("claim_profile", ""),
            "claim_text": row.get("selected_claim_text", ""),
            "subject": row.get("selected_subject", ""),
            "predicate": row.get("selected_predicate", ""),
            "object": row.get("selected_object", ""),
            "context_summary": summarize_context(row.get("extracted_context_json", {})),
            "context_json": serialize_export_value(row.get("extracted_context_json", {})),
            "details_summary": summarize_details(row.get("extracted_details_json", {})),
            "details_json": serialize_export_value(row.get("extracted_details_json", {})),
            "extractor_metadata_summary": summarize_details(extractor_metadata_json),
            "extractor_metadata_json": serialize_export_value(extractor_metadata_json),
            "section_summary_json": serialize_export_value(row.get("section_summary_json", {})),
            "paper_summary_json": serialize_export_value(row.get("paper_summary_json", {})),
            "paper_claim_registry_json": serialize_export_value(row.get("paper_claim_registry_json", [])),
            "paper_evidence_registry_json": serialize_export_value(row.get("paper_evidence_registry_json", [])),
            "linked_evidence_ids": row.get("linked_evidence_ids", ""),
            "evidence_summary": summarize_evidence_items(row.get("group_evidence_items_json", [])),
            "evidence_items_json": serialize_export_value(row.get("group_evidence_items_json", [])),
            "claim_evidence_links_json": serialize_export_value(row.get("group_links_json", [])),
        }
        if mode == "gold":
            output_rows.append(
                {
                    **common,
                    "review_group_id": row.get("group_id", ""),
                    "review_section_name": row.get("section_title", ""),
                    "review_source_quote": row.get("source_quote", ""),
                    "match_score": row.get("match_score", ""),
                    **judge_fields,
                }
            )
        else:
            output_rows.append(
                {
                    **common,
                    "section_text": row.get("source_quote", ""),
                    **judge_fields,
                }
            )
    fieldnames = (
        GOLD_EVALUATION_ROW_FIELDS if mode == "gold" else INTRINSIC_EVALUATION_ROW_FIELDS
    ) + _judge_fields(rows)
    write_reviewer_rows(output_rows, output_path, fieldnames, xlsx=xlsx)


def _judge_fields(rows: list[dict[str, Any]]) -> list[str]:
    fieldnames: list[str] = []
    if not rows:
        return fieldnames
    for key in rows[0]:
        if key.startswith("llm_judge_") and key not in fieldnames:
            fieldnames.append(key)
    return fieldnames


def append_judge_fields(rows: list[dict[str, Any]], judged_fields: list[dict[str, str]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row, judge in zip(rows, judged_fields):
        combined = dict(row)
        combined.update(judge)
        merged.append(combined)
    return merged


def write_csv_rows(output_path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
