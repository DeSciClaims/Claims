from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .models import OntologyMappingRecord, ValidationIssue


MAPPING_ROW_FIELDS = [
    "paper_id",
    "object_type",
    "object_id",
    "claim_profile",
    "evidence_method",
    "object_text",
    "field_path",
    "field_role",
    "raw_text",
    "normalized_text",
    "normalization_status",
    "skip_reason",
    "entity_type",
    "routed_sources",
    "mapping_status",
    "mapping_method",
    "selected_ontology_source",
    "selected_ontology_id",
    "selected_ontology_label",
    "candidate_count",
    "candidate_mappings_json",
    "metadata_json",
]

VALIDATION_ROW_FIELDS = [
    "paper_id",
    "object_type",
    "object_id",
    "severity",
    "code",
    "field_path",
    "message",
    "observed_value",
    "expected",
]


def write_json(output_path: Path, payload: Any) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_mapping_rows(output_path: Path, records: list[OntologyMappingRecord]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for record in records:
        annotation = record.annotation
        selected = annotation.selected_mapping if annotation else None
        rows.append(
            {
                "paper_id": record.paper_id,
                "object_type": record.object_type,
                "object_id": record.object_id,
                "claim_profile": record.claim_profile or "",
                "evidence_method": record.evidence_method or "",
                "object_text": record.object_text,
                "field_path": record.field_path,
                "field_role": record.field_role or "",
                "raw_text": record.raw_text,
                "normalized_text": record.normalized_text or (annotation.normalized_text if annotation else ""),
                "normalization_status": record.normalization_status or "",
                "skip_reason": record.skip_reason or "",
                "entity_type": record.entity_type or "",
                "routed_sources": ",".join(record.routed_sources),
                "mapping_status": record.mapping_status or (annotation.mapping_status if annotation else ""),
                "mapping_method": record.mapping_method or (annotation.mapping_method if annotation else ""),
                "selected_ontology_source": selected.ontology_source if selected else "",
                "selected_ontology_id": selected.ontology_id if selected else "",
                "selected_ontology_label": selected.ontology_label if selected else "",
                "candidate_count": record.candidate_count,
                "candidate_mappings_json": json.dumps(
                    [candidate.model_dump(mode="json") for candidate in (annotation.candidate_mappings if annotation else [])],
                    ensure_ascii=False,
                ),
                "metadata_json": json.dumps(record.metadata, ensure_ascii=False),
            }
        )
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_ROW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_validation_rows(output_path: Path, issues: list[ValidationIssue]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "paper_id": issue.paper_id,
            "object_type": issue.object_type,
            "object_id": issue.object_id,
            "severity": issue.severity,
            "code": issue.code,
            "field_path": issue.field_path or "",
            "message": issue.message,
            "observed_value": issue.observed_value or "",
            "expected": issue.expected or "",
        }
        for issue in issues
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VALIDATION_ROW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
