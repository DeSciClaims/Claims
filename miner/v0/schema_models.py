from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class OntologyCandidate(BaseModel):
    ontology_source: str
    ontology_id: str
    ontology_label: str
    match_type: Optional[str] = None
    confidence: Optional[float] = None


class OntologyAnnotation(BaseModel):
    annotation_type: Literal["mapping", "classification"]
    raw_text: Optional[str] = None
    normalized_text: Optional[str] = None
    mapping_status: Literal["mapped", "ambiguous", "unresolved", "rejected"]
    candidate_mappings: List[OntologyCandidate] = Field(default_factory=list)
    selected_mapping: Optional[OntologyCandidate] = None
    mapping_method: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_model_enum_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        annotation_type = str(normalized.get("annotation_type") or "").strip().lower()
        if annotation_type not in {"mapping", "classification"}:
            annotation_type = "classification"
        normalized["annotation_type"] = annotation_type

        mapping_status = str(normalized.get("mapping_status") or "").strip().lower()
        status_aliases = {
            "mapped": "mapped",
            "match": "mapped",
            "matched": "mapped",
            "resolved": "mapped",
            "ambiguous": "ambiguous",
            "uncertain": "ambiguous",
            "unresolved": "unresolved",
            "unknown": "unresolved",
            "": "unresolved",
            "rejected": "rejected",
            "reject": "rejected",
        }
        normalized["mapping_status"] = status_aliases.get(mapping_status, "unresolved")
        return normalized


class SemanticField(BaseModel):
    value: str
    entity_type: Optional[str] = None
    ontology: Optional[OntologyAnnotation] = None


class Paper(BaseModel):
    paper_id: str
    doi: Optional[str] = None
    pmid: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = Field(default_factory=list)
    journal: Optional[str] = None
    year: Optional[int] = None
    source_type: Optional[str] = None
    open_access: Optional[bool] = None


class Span(BaseModel):
    span_id: str
    paper_id: str
    section_type: Optional[str] = None
    section_name: Optional[str] = None
    page: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    text: str
    table_id: Optional[str] = None
    figure_id: Optional[str] = None
    caption_id: Optional[str] = None
    span_type: Literal["text", "table_cell", "table_row", "figure_caption", "other"] = "text"


class Claim(BaseModel):
    claim_id: str
    paper_id: str
    claim_text: str
    subject: SemanticField = Field(default_factory=lambda: SemanticField(value="", entity_type="v0_compat"))
    predicate: SemanticField = Field(default_factory=lambda: SemanticField(value="", entity_type="v0_compat"))
    object: SemanticField = Field(default_factory=lambda: SemanticField(value="", entity_type="v0_compat"))
    claim_kind: str = "paper_claim"
    claim_profile: Optional[str] = None
    epistemic_status: str = "asserted_by_paper"
    support_origin: str = "own_work"
    source_span_ids: List[str] = Field(default_factory=list)
    context: Dict[str, SemanticField] = Field(default_factory=dict)
    details: Dict[str, Any] = Field(default_factory=dict)
    extractor_confidence: Optional[float] = None

    @field_validator("subject", "predicate", "object", mode="before")
    @classmethod
    def _coerce_required_semantic_fields(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"value": value}
        return value


class EvidenceItem(BaseModel):
    evidence_id: str
    paper_id: str
    role: str
    summary_text: str
    evidence_method: SemanticField = Field(
        default_factory=lambda: SemanticField(value="textual_evidence", entity_type="evidence_method")
    )
    outcome_type: Optional[SemanticField] = None
    presentation_type: Optional[SemanticField] = None
    source_span_ids: List[str] = Field(default_factory=list)
    context: Dict[str, SemanticField] = Field(default_factory=dict)
    details: Dict[str, Any] = Field(default_factory=dict)
    ontology: Optional[OntologyAnnotation] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_evidence_type(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("evidence_method") is not None:
            return data
        evidence_type = data.get("evidence_type")
        if not evidence_type:
            return data
        migrated = dict(data)
        migrated.pop("evidence_type", None)
        if evidence_type == "clinical_outcome":
            migrated["evidence_method"] = {"value": "observation", "entity_type": "evidence_method"}
            migrated["outcome_type"] = {"value": "clinical_outcome", "entity_type": "outcome_type"}
            migrated["presentation_type"] = {"value": "text", "entity_type": "presentation_type"}
        elif evidence_type == "table_result":
            migrated["evidence_method"] = {"value": "observation", "entity_type": "evidence_method"}
            migrated["outcome_type"] = {"value": "quantitative_measure", "entity_type": "outcome_type"}
            migrated["presentation_type"] = {"value": "table", "entity_type": "presentation_type"}
        elif evidence_type == "figure_result":
            migrated["evidence_method"] = {"value": "observation", "entity_type": "evidence_method"}
            migrated["outcome_type"] = {"value": "quantitative_measure", "entity_type": "outcome_type"}
            migrated["presentation_type"] = {"value": "figure", "entity_type": "presentation_type"}
        elif evidence_type == "text_statement":
            migrated["evidence_method"] = {"value": "observation", "entity_type": "evidence_method"}
            migrated["presentation_type"] = {"value": "text", "entity_type": "presentation_type"}
        else:
            migrated["evidence_method"] = {"value": evidence_type, "entity_type": "evidence_method"}
            migrated["presentation_type"] = {"value": "text", "entity_type": "presentation_type"}
        return migrated

    @field_validator("evidence_method", mode="before")
    @classmethod
    def _coerce_evidence_method(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"value": value, "entity_type": "evidence_method"}
        if isinstance(value, dict) and "entity_type" not in value:
            updated = dict(value)
            updated["entity_type"] = "evidence_method"
            return updated
        return value

    @field_validator("outcome_type", mode="before")
    @classmethod
    def _coerce_outcome_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"value": value, "entity_type": "outcome_type"}
        if isinstance(value, dict) and "entity_type" not in value:
            updated = dict(value)
            updated["entity_type"] = "outcome_type"
            return updated
        return value

    @field_validator("presentation_type", mode="before")
    @classmethod
    def _coerce_presentation_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"value": value, "entity_type": "presentation_type"}
        if isinstance(value, dict) and "entity_type" not in value:
            updated = dict(value)
            updated["entity_type"] = "presentation_type"
            return updated
        return value


class ClaimEvidenceLink(BaseModel):
    link_id: str
    claim_id: str
    evidence_id: str
    relation: str
    confidence: Optional[float] = None


class ExtractionArtifact(BaseModel):
    paper: Paper
    spans: List[Span] = Field(default_factory=list)
    claims: List[Claim] = Field(default_factory=list)
    evidence_items: List[EvidenceItem] = Field(default_factory=list)
    claim_evidence_links: List[ClaimEvidenceLink] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("claims", "evidence_items", "claim_evidence_links")
    @classmethod
    def _default_list(cls, value: Optional[List[Any]]) -> List[Any]:
        return value or []
