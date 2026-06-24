from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SectionRecord(BaseModel):
    section_id: str
    paper_id: str
    section_name: str
    section_type: str
    section_source: str = "tei_header"
    section_title_quality: str = "high"
    original_section_name: str | None = None
    page_numbers: list[int] = Field(default_factory=list)
    span_ids: list[str] = Field(default_factory=list)
    text: str
    token_count: int = 0
    char_count: int = 0


class SectionSummaryRecord(BaseModel):
    section_id: str
    section_name: str
    section_type: str
    summary_text: str
    section_role: str
    key_entities: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    extractability_assessment: str = ""
    locality_confidence: float | None = None


class PaperSummaryRecord(BaseModel):
    paper_id: str
    paper_title: str = ""
    paper_summary: str
    main_findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_map: list[str] = Field(default_factory=list)


class SectionExtractionDecision(BaseModel):
    section_id: str
    should_extract: bool
    reason: str
    expected_claim_types: list[str] = Field(default_factory=list)
    expected_evidence_types: list[str] = Field(default_factory=list)
    likely_claim_density: str = ""
    likely_evidence_density: str = ""
    likely_context_completeness: str = ""


class ExtractionBundle(BaseModel):
    section: SectionRecord
    section_summary: SectionSummaryRecord
    paper_summary: PaperSummaryRecord
    decision: SectionExtractionDecision
    raw_output: dict[str, Any]


class EvaluatedClaimMatch(BaseModel):
    paper_id: str
    group_id: str
    section_title: str
    source_quote: str
    matched_section_id: str | None = None
    matched_section_name: str | None = None
    match_score: float = 0.0
    claim_id: str | None = None
    claim_profile: str = ""
    selected_claim_text: str = ""
    selected_subject: str = ""
    selected_predicate: str = ""
    selected_object: str = ""
    extracted_context_json: dict[str, Any] = Field(default_factory=dict)
    extracted_details_json: dict[str, Any] = Field(default_factory=dict)
    extractor_metadata_json: dict[str, Any] = Field(default_factory=dict)
    linked_evidence_ids: str = ""
    group_evidence_items_json: list[dict[str, Any]] = Field(default_factory=list)
    group_links_json: list[dict[str, Any]] = Field(default_factory=list)
