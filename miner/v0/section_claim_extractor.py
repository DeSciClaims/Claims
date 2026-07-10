from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .id_factory import stable_id
from .schema_models import (
    Claim,
    ClaimEvidenceLink,
    EvidenceItem,
    SemanticField,
)

from .models import PaperSummaryRecord, SectionRecord, SectionSummaryRecord

if TYPE_CHECKING:
    from .dspy_runtime import SectionContextV1DSPyRuntime


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "section_claim_extraction_instructions.md"
CANDIDATE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "section_candidate_extraction_instructions.md"
ATOMICITY_REPAIR_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "section_atomicity_repair_instructions.md"
logger = logging.getLogger(__name__)


def create_section_candidate_extractor_program(dspy_module, *, instructions: str):
    class SectionCandidateExtractionSignature(dspy_module.Signature):
        """Extract raw candidate spans from raw section text. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        section_summary_json: str = dspy_module.InputField()
        section_name: str = dspy_module.InputField()
        section_type: str = dspy_module.InputField()
        section_text: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(SectionCandidateExtractionSignature)
    predictor.signature.instructions = instructions
    return predictor


def create_section_claim_extractor_program(dspy_module, *, instructions: str):
    class SectionClaimExtractionSignature(dspy_module.Signature):
        """Classify candidates and extract section-local claim-evidence pairs. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        section_summary_json: str = dspy_module.InputField()
        section_name: str = dspy_module.InputField()
        section_type: str = dspy_module.InputField()
        section_text: str = dspy_module.InputField()
        candidate_spans_json: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(SectionClaimExtractionSignature)
    predictor.signature.instructions = instructions
    return predictor


def create_section_atomicity_repair_program(dspy_module, *, instructions: str):
    class SectionAtomicityRepairSignature(dspy_module.Signature):
        """Repair compound section-local claim-evidence pairs. Return STRICT JSON ONLY."""

        section_name: str = dspy_module.InputField()
        section_type: str = dspy_module.InputField()
        section_text: str = dspy_module.InputField()
        candidate_spans_json: str = dspy_module.InputField()
        classified_spans_json: str = dspy_module.InputField()
        decomposed_units_json: str = dspy_module.InputField()
        claims_json: str = dspy_module.InputField()
        evidence_items_json: str = dspy_module.InputField()
        claim_evidence_links_json: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(SectionAtomicityRepairSignature)
    predictor.signature.instructions = instructions
    return predictor


def load_section_claim_extraction_instructions() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_section_candidate_extraction_instructions() -> str:
    return CANDIDATE_PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_section_atomicity_repair_instructions() -> str:
    return ATOMICITY_REPAIR_PROMPT_PATH.read_text(encoding="utf-8").strip()


def extract_section_claims(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    section: SectionRecord,
    section_summary: SectionSummaryRecord,
) -> tuple[list[Claim], list[EvidenceItem], list[ClaimEvidenceLink], dict[str, Any]]:
    candidate_output = _predict_section_candidates(
        predictor=runtime.section_candidate_extractor_program,
        paper_title=paper_title,
        paper_summary=paper_summary,
        section=section,
        section_summary=section_summary,
    )
    candidate_spans = _normalize_candidate_spans(candidate_output.get("candidate_spans", []))
    raw_output = _predict_section_claims(
        predictor=runtime.section_claim_extractor_program,
        paper_title=paper_title,
        paper_summary=paper_summary,
        section=section,
        section_summary=section_summary,
        candidate_spans=candidate_spans,
        validation_feedback={},
    )
    raw_output["candidate_spans"] = candidate_spans
    raw_output = _repair_section_claim_atomicity(
        runtime=runtime,
        section=section,
        raw_output=raw_output,
    )
    claims = _materialize_claims(section, raw_output.get("claims", []))
    evidence_items = _materialize_evidence_items(section, raw_output.get("evidence_items", []))
    links = _materialize_links(claims, evidence_items, raw_output.get("claim_evidence_links", []))
    return claims, evidence_items, links, raw_output


def _repair_section_claim_atomicity(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    section: SectionRecord,
    raw_output: dict[str, Any],
) -> dict[str, Any]:
    original_claims = raw_output.get("claims", [])
    original_evidence_items = raw_output.get("evidence_items", [])
    original_links = raw_output.get("claim_evidence_links", [])
    if not original_claims or not original_evidence_items or not original_links:
        return raw_output

    repair_output = _predict_atomicity_repair(
        predictor=runtime.section_atomicity_repair_program,
        runtime=runtime,
        section=section,
        candidate_spans=raw_output.get("candidate_spans", []),
        classified_spans=raw_output.get("classified_spans", []),
        decomposed_units=raw_output.get("decomposed_units", []),
        claims=original_claims,
        evidence_items=original_evidence_items,
        claim_evidence_links=original_links,
        validation_feedback={},
    )
    if not _repair_output_is_usable(repair_output):
        updated = dict(raw_output)
        updated["atomicity_repair_actions"] = [
            {
                "action": "repair_parse_failed",
                "reason": "Atomicity repair output was missing claims, evidence_items, or claim_evidence_links; original extraction kept.",
            }
        ]
        return updated

    repair_issues = _find_repair_issues(repair_output)
    if repair_issues:
        retry_output = _predict_atomicity_repair(
            predictor=runtime.section_atomicity_repair_program,
            runtime=runtime,
            section=section,
            candidate_spans=raw_output.get("candidate_spans", []),
            classified_spans=raw_output.get("classified_spans", []),
            decomposed_units=raw_output.get("decomposed_units", []),
            claims=repair_output.get("claims", []),
            evidence_items=repair_output.get("evidence_items", []),
            claim_evidence_links=repair_output.get("claim_evidence_links", []),
            validation_feedback={
                "repair_issues": repair_issues,
                "instruction": "Fix these exact remaining issues. Return the full revised claim/evidence/link set.",
            },
        )
        if _repair_output_is_usable(retry_output):
            retry_issues = _find_repair_issues(retry_output)
            if len(retry_issues) <= len(repair_issues):
                retry_actions = retry_output.get("repair_actions", [])
                retry_actions.append(
                    {
                        "action": "repair_retry",
                        "reason": "A second repair pass was run because validation found remaining claim/evidence separation issues.",
                        "remaining_issue_count": len(retry_issues),
                    }
                )
                retry_output["repair_actions"] = retry_actions
                repair_output = retry_output

    updated = dict(raw_output)
    updated["pre_atomicity_repair"] = {
        "claims": original_claims,
        "evidence_items": original_evidence_items,
        "claim_evidence_links": original_links,
    }
    updated["atomicity_repair_actions"] = repair_output.get("repair_actions", [])
    updated["claims"] = repair_output.get("claims", [])
    updated["evidence_items"] = repair_output.get("evidence_items", [])
    updated["claim_evidence_links"] = repair_output.get("claim_evidence_links", [])
    return updated


def _predict_atomicity_repair(
    *,
    predictor: Any,
    runtime: "SectionContextV1DSPyRuntime",
    section: SectionRecord,
    candidate_spans: Any,
    classified_spans: Any,
    decomposed_units: Any,
    claims: Any,
    evidence_items: Any,
        claim_evidence_links: Any,
        validation_feedback: dict[str, Any],
) -> dict[str, Any]:
    with runtime.dspy_module.context(lm=runtime.repair_lm):
        prediction = predictor(
            section_name=section.section_name,
            section_type=section.section_type,
            section_text=section.text,
            candidate_spans_json=json.dumps(candidate_spans or [], ensure_ascii=False),
            classified_spans_json=json.dumps(classified_spans or [], ensure_ascii=False),
            decomposed_units_json=json.dumps(decomposed_units or [], ensure_ascii=False),
            claims_json=json.dumps(claims or [], ensure_ascii=False),
            evidence_items_json=json.dumps(evidence_items or [], ensure_ascii=False),
            claim_evidence_links_json=json.dumps(claim_evidence_links or [], ensure_ascii=False),
            validation_feedback_json=json.dumps(validation_feedback or {}, ensure_ascii=False),
        )
    return _safe_json_loads(getattr(prediction, "json_output", ""))


def _repair_output_is_usable(output: dict[str, Any]) -> bool:
    claims = output.get("claims")
    evidence_items = output.get("evidence_items")
    links = output.get("claim_evidence_links")
    if not isinstance(claims, list) or not isinstance(evidence_items, list) or not isinstance(links, list):
        return False
    if not claims or not evidence_items or not links:
        return False
    return True


def _find_repair_issues(output: dict[str, Any]) -> list[dict[str, Any]]:
    claims = output.get("claims") if isinstance(output.get("claims"), list) else []
    evidence_items = output.get("evidence_items") if isinstance(output.get("evidence_items"), list) else []
    links = output.get("claim_evidence_links") if isinstance(output.get("claim_evidence_links"), list) else []
    evidence_by_index = {idx: item for idx, item in enumerate(evidence_items) if isinstance(item, dict)}
    issues: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        claim_text = str(claim.get("claim_text", "")).strip()
        if not claim_text:
            continue
        identifier_count = len(set(re.findall(r"\b(?:rs\d+|[A-Z][A-Z0-9-]{2,})\b", claim_text)))
        if identifier_count > 1 and re.search(r"\b(?:and|two|three|four|respectively)\b", claim_text, re.I):
            issues.append(
                {
                    "claim_index": claim_index,
                    "issue": "bundled_multiple_identifiers",
                    "claim_text": claim_text,
                    "instruction": "Split this into one claim per identifier/entity unless the proposition is explicitly about the group.",
                }
            )
        if re.search(
            r"\b(?:P\s*[=<>]|p\s*[=<>]|odds ratio|OR\s*=|R2|R\^2|% of variance|percentage-point|confidence interval|SE\s*=)\b",
            claim_text,
            re.I,
        ):
            issues.append(
                {
                    "claim_index": claim_index,
                    "issue": "claim_contains_support_statistic",
                    "claim_text": claim_text,
                    "instruction": "Move routine support statistics or effect magnitudes into linked evidence.",
                }
            )
        normalized_claim = _normalize_for_overlap(claim_text)
        for link in links:
            if not isinstance(link, dict) or _coerce_index(link.get("claim_index"), len(claims)) != claim_index:
                continue
            evidence_index = _coerce_index(link.get("evidence_index"), len(evidence_items))
            if evidence_index is None:
                continue
            evidence = evidence_by_index.get(evidence_index, {})
            evidence_text = str(evidence.get("summary_text", "")).strip()
            normalized_evidence = _normalize_for_overlap(evidence_text)
            if normalized_claim and (
                normalized_claim == normalized_evidence
                or normalized_claim in normalized_evidence
                or normalized_evidence in normalized_claim
            ):
                issues.append(
                    {
                        "claim_index": claim_index,
                        "evidence_index": evidence_index,
                        "issue": "evidence_repeats_claim",
                        "claim_text": claim_text,
                        "evidence_text": evidence_text,
                        "instruction": "Rewrite claim as proposition-only and evidence as source-side support.",
                    }
                )
    return issues


def _normalize_for_overlap(value: str) -> str:
    return " ".join(value.lower().split())


def _predict_section_candidates(
    *,
    predictor: Any,
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    section: SectionRecord,
    section_summary: SectionSummaryRecord,
) -> dict[str, Any]:
    prediction = predictor(
        paper_title=paper_title,
        paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
        section_summary_json=json.dumps(section_summary.model_dump(mode="json"), ensure_ascii=False),
        section_name=section.section_name,
        section_type=section.section_type,
        section_text=section.text,
    )
    return _safe_json_loads(getattr(prediction, "json_output", ""))


def _predict_section_claims(
    *,
    predictor: Any,
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    section: SectionRecord,
    section_summary: SectionSummaryRecord,
    candidate_spans: list[dict[str, Any]],
    validation_feedback: dict[str, Any],
) -> dict[str, Any]:
    prediction = predictor(
        paper_title=paper_title,
        paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
        section_summary_json=json.dumps(section_summary.model_dump(mode="json"), ensure_ascii=False),
        section_name=section.section_name,
        section_type=section.section_type,
        section_text=section.text,
        candidate_spans_json=json.dumps(candidate_spans, ensure_ascii=False),
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


def _normalize_candidate_spans(raw_candidates: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list):
        return []
    candidates: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            continue
        source_text = str(raw.get("source_text", "")).strip()
        if not source_text:
            continue
        candidate_id = str(raw.get("candidate_id") or f"c{index}").strip()
        if not candidate_id or candidate_id in used_ids:
            candidate_id = f"c{index}"
        used_ids.add(candidate_id)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "source_text": source_text,
                "initial_role_hint": str(raw.get("initial_role_hint", "unclear")).strip() or "unclear",
                "reason": str(raw.get("reason", "")).strip(),
            }
        )
    return candidates


def _materialize_claims(section: SectionRecord, raw_claims: list[Any]) -> list[Claim]:
    claims: list[Claim] = []
    for index, raw in enumerate(raw_claims or []):
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim_text", "")).strip()
        if not claim_text:
            continue
        claim = Claim(
            claim_id=stable_id("claim", section.paper_id, section.section_id, str(index), claim_text),
            paper_id=section.paper_id,
            claim_text=claim_text,
            subject=_semantic_field(raw.get("subject"), default_entity_type="v0_compat"),
            predicate=_semantic_field(raw.get("predicate"), default_entity_type="v0_compat"),
            object=_semantic_field(raw.get("object"), default_entity_type="v0_compat"),
            claim_kind=str(raw.get("claim_kind", "paper_claim")).strip() or "paper_claim",
            claim_profile=None,
            claim_subtype=_optional_str(raw.get("claim_subtype")),
            modality=_optional_str(raw.get("modality")),
            polarity=_optional_str(raw.get("polarity")),
            attribution=_optional_str(raw.get("attribution")),
            epistemic_status=str(raw.get("epistemic_status", "asserted_by_paper")).strip() or "asserted_by_paper",
            support_origin=str(raw.get("support_origin", "own_work")).strip() or "own_work",
            source_span_ids=list(section.span_ids),
            source_candidate_ids=_str_list(raw.get("source_candidate_ids")),
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
                evidence_type=_optional_str(raw.get("evidence_type")),
                rhetorical_role=_optional_str(raw.get("rhetorical_role")),
                evidence_method=evidence_method,
                outcome_type=raw.get("outcome_type"),
                presentation_type=raw.get("presentation_type") or {"value": "text", "entity_type": "presentation_type"},
                source_span_ids=list(section.span_ids),
                source_candidate_ids=_str_list(raw.get("source_candidate_ids")),
                context={},
                details={},
                ontology=None,
                extractor_confidence=_coerce_float(raw.get("extractor_confidence")),
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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _semantic_field(
    value: Any,
    *,
    default_entity_type: str,
) -> SemanticField:
    if isinstance(value, dict):
        raw = value.get("value", "")
        entity_type = value.get("entity_type") or default_entity_type
    else:
        raw = value or ""
        entity_type = default_entity_type
    return SemanticField(value=str(raw or "").strip(), entity_type=str(entity_type or default_entity_type), ontology=None)
