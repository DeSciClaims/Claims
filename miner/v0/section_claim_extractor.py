from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .id_factory import stable_id
from .profiles import (
    EVIDENCE_METHOD_PROFILES,
    OUTCOME_TYPE_PROFILES,
    PRESENTATION_TYPE_PROFILES,
    evidence_method_prompt_json,
)
from .schema_models import (
    Claim,
    ClaimEvidenceLink,
    EvidenceItem,
    OntologyAnnotation,
    SemanticField,
)

from .models import PaperSummaryRecord, SectionRecord, SectionSummaryRecord

if TYPE_CHECKING:
    from .dspy_runtime import SectionContextV1DSPyRuntime


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "section_claim_extraction_instructions.md"
logger = logging.getLogger(__name__)


def create_section_claim_extractor_program(dspy_module, *, instructions: str):
    class SectionClaimExtractionSignature(dspy_module.Signature):
        """Extract section-local claims from raw section text. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        section_summary_json: str = dspy_module.InputField()
        section_name: str = dspy_module.InputField()
        section_type: str = dspy_module.InputField()
        section_text: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(SectionClaimExtractionSignature)
    predictor.signature.instructions = instructions
    return predictor


def load_section_claim_extraction_instructions() -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8").strip()
    evidence_context_keys = sorted(
        {
            key
            for profile in EVIDENCE_METHOD_PROFILES.values()
            for key in profile.get("allowed_context_keys", [])
        }
    )
    evidence_detail_keys = sorted(
        {
            key
            for profile in EVIDENCE_METHOD_PROFILES.values()
            for key in profile.get("allowed_details_keys", [])
        }
    )
    return (
        template.replace("__EVIDENCE_METHOD_SPECS__", evidence_method_prompt_json())
        .replace("__EVIDENCE_METHOD_VALUES__", ", ".join(EVIDENCE_METHOD_PROFILES.keys()))
        .replace("__OUTCOME_TYPE_VALUES__", ", ".join(OUTCOME_TYPE_PROFILES.keys()))
        .replace("__PRESENTATION_TYPE_VALUES__", ", ".join(PRESENTATION_TYPE_PROFILES.keys()))
        .replace("__EVIDENCE_CONTEXT_KEYS__", ", ".join(evidence_context_keys))
        .replace("__EVIDENCE_DETAIL_KEYS__", ", ".join(evidence_detail_keys))
    )


def extract_section_claims(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    section: SectionRecord,
    section_summary: SectionSummaryRecord,
) -> tuple[list[Claim], list[EvidenceItem], list[ClaimEvidenceLink], dict[str, Any]]:
    predictor = runtime.section_claim_extractor_program
    raw_output = _predict_section_claims(
        predictor=predictor,
        paper_title=paper_title,
        paper_summary=paper_summary,
        section=section,
        section_summary=section_summary,
        validation_feedback={},
    )
    claims = _materialize_claims(section, raw_output.get("claims", []))
    evidence_items = _materialize_evidence_items(section, raw_output.get("evidence_items", []))
    links = _materialize_links(claims, evidence_items, raw_output.get("claim_evidence_links", []))
    return claims, evidence_items, links, raw_output


def _predict_section_claims(
    *,
    predictor: Any,
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    section: SectionRecord,
    section_summary: SectionSummaryRecord,
    validation_feedback: dict[str, Any],
) -> dict[str, Any]:
    prediction = predictor(
        paper_title=paper_title,
        paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
        section_summary_json=json.dumps(section_summary.model_dump(mode="json"), ensure_ascii=False),
        section_name=section.section_name,
        section_type=section.section_type,
        section_text=section.text,
        validation_feedback_json=json.dumps(validation_feedback, ensure_ascii=False),
    )
    return _safe_json_loads(getattr(prediction, "json_output", ""))


def _safe_json_loads(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_output)
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _materialize_claims(section: SectionRecord, raw_claims: list[Any]) -> list[Claim]:
    claims: list[Claim] = []
    for index, raw in enumerate(raw_claims or []):
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim_text", "")).strip()
        if not claim_text:
            continue
        claim_profile = str(raw.get("claim_profile") or "claim_text_v0").strip() or "claim_text_v0"
        claim = Claim(
            claim_id=stable_id("claim", section.paper_id, section.section_id, str(index), claim_text),
            paper_id=section.paper_id,
            claim_text=claim_text,
            subject=_semantic_field(raw.get("subject"), default_entity_type="v0_compat"),
            predicate=_semantic_field(raw.get("predicate"), default_entity_type="v0_compat"),
            object=_semantic_field(raw.get("object"), default_entity_type="v0_compat"),
            claim_kind=str(raw.get("claim_kind", "paper_claim")).strip() or "paper_claim",
            claim_profile=claim_profile,
            epistemic_status=str(raw.get("epistemic_status", "asserted_by_paper")).strip() or "asserted_by_paper",
            support_origin=str(raw.get("support_origin", "own_work")).strip() or "own_work",
            source_span_ids=list(section.span_ids),
            context={},
            details={},
            extractor_confidence=_coerce_float(raw.get("extractor_confidence")),
        )
        claims.append(claim)
    return claims


def _materialize_evidence_items(section: SectionRecord, raw_evidence_items: list[Any]) -> list[EvidenceItem]:
    evidence_items: list[EvidenceItem] = []
    for index, raw in enumerate(raw_evidence_items or []):
        if not isinstance(raw, dict):
            continue
        summary_text = str(raw.get("summary_text", "")).strip()
        if not summary_text:
            continue
        try:
            evidence_method = raw.get("evidence_method") or {"value": "textual_evidence", "entity_type": "evidence_method"}
            evidence = EvidenceItem(
                evidence_id=stable_id("evidence", section.paper_id, section.section_id, str(index), summary_text),
                paper_id=section.paper_id,
                role=str(raw.get("role", "supports")).strip() or "supports",
                summary_text=summary_text,
                evidence_method=evidence_method,
                outcome_type=raw.get("outcome_type"),
                presentation_type=raw.get("presentation_type") or {"value": "text", "entity_type": "presentation_type"},
                source_span_ids=list(section.span_ids),
                context={},
                details={},
                ontology=None,
            )
        except Exception as exc:
            logger.warning(
                "section_context_v1: skipping evidence item %s in section `%s` due to validation error: %s",
                index,
                section.section_name or section.section_id,
                exc,
            )
            continue
        evidence_items.append(evidence)
    return evidence_items


def _materialize_links(
    claims: list[Claim],
    evidence_items: list[EvidenceItem],
    raw_links: list[Any],
) -> list[ClaimEvidenceLink]:
    links: list[ClaimEvidenceLink] = []
    for raw in raw_links or []:
        if not isinstance(raw, dict):
            continue
        claim_index = _coerce_index(raw.get("claim_index"), len(claims))
        evidence_index = _coerce_index(raw.get("evidence_index"), len(evidence_items))
        if claim_index is None or evidence_index is None:
            continue
        claim = claims[claim_index]
        evidence = evidence_items[evidence_index]
        relation = str(raw.get("relation", "supports")).strip() or "supports"
        links.append(
            ClaimEvidenceLink(
                link_id=stable_id("claim_evidence_link", claim.claim_id, evidence.evidence_id, relation),
                claim_id=claim.claim_id,
                evidence_id=evidence.evidence_id,
                relation=relation,
                confidence=_coerce_float(raw.get("confidence")),
            )
        )
    return links


def _coerce_index(value: object, upper_bound: int) -> int | None:
    try:
        index = int(value)
    except Exception:
        return None
    if index < 0 or index >= upper_bound:
        return None
    return index


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _semantic_field(
    value: Any,
    *,
    default_entity_type: str,
    field_path: str | None = None,
    claim_profile: str | None = None,
) -> SemanticField:
    from .reviewer_export_utils import normalize_semantic_field

    normalized = normalize_semantic_field(
        value,
        default_entity_type=default_entity_type,
        field_path=field_path,
        claim_profile=claim_profile,
    )
    return SemanticField(
        value=normalized["value"],
        entity_type=normalized["entity_type"],
        ontology=_ontology_annotation(normalized.get("ontology")),
    )


def _ontology_annotation(value: Any) -> OntologyAnnotation | dict[str, Any] | None:
    if value in (None, "", {}):
        return None
    if isinstance(value, OntologyAnnotation):
        return value
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {
            "annotation_type": "classification",
            "raw_text": text,
            "normalized_text": text,
            "mapping_status": "unresolved",
            "candidate_mappings": [],
            "selected_mapping": None,
            "mapping_method": "section_context_v1_string_fallback",
        }
    return None
