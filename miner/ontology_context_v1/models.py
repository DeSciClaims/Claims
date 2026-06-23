from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..section_context_v1.schema_models import OntologyAnnotation


class FieldNormalization(BaseModel):
    raw_text: str
    normalized_text: str
    field_role: str
    normalization_rule: str | None = None
    normalization_status: str = "unchanged"
    should_attempt_mapping: bool = False
    skip_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    semantic_payload_type: str | None = None


class OntologyMappingRecord(BaseModel):
    record_id: str
    paper_id: str
    object_type: str
    object_id: str
    object_text: str = ""
    field_path: str
    raw_text: str
    normalized_text: str | None = None
    field_role: str | None = None
    normalization_status: str | None = None
    skip_reason: str | None = None
    entity_type: str | None = None
    claim_profile: str | None = None
    evidence_method: str | None = None
    routed_sources: list[str] = Field(default_factory=list)
    annotation: OntologyAnnotation | None = None
    mapping_status: str | None = None
    mapping_method: str | None = None
    candidate_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    issue_id: str
    paper_id: str
    object_type: str
    object_id: str
    severity: str
    code: str
    field_path: str | None = None
    message: str
    observed_value: str | None = None
    expected: str | None = None


class ValidationSummary(BaseModel):
    paper_id: str
    pipeline_name: str
    pipeline_role: str
    issue_count: int
    severity_counts: dict[str, int] = Field(default_factory=dict)
    code_counts: dict[str, int] = Field(default_factory=dict)
