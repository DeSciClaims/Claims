from __future__ import annotations

from typing import Any


VALID_RELATIONS = {
    "supports",
    "contradicts",
    "qualifies",
    "backgrounds",
}


def validate_against_schema(payload: dict[str, Any], schema_name: str) -> list[str]:
    validators = {
        "paper.schema.json": _validate_paper,
        "span.schema.json": _validate_span,
        "claim.schema.json": _validate_claim,
        "evidence_item.schema.json": _validate_evidence_item,
        "claim_evidence_link.schema.json": _validate_claim_evidence_link,
        "extraction.schema.json": _validate_extraction,
        "validator_score.schema.json": _validate_validator_score,
    }
    validator = validators.get(schema_name)
    if validator is None:
        return [f"Unsupported schema: {schema_name}"]
    return validator(payload)


def structure_score(payload: dict[str, Any]) -> float:
    claims = payload.get("claims", [])
    evidence_items = payload.get("evidence_items", [])
    links = payload.get("claim_evidence_links", [])
    if not claims or not evidence_items or not links:
        return 0.0
    return 1.0


def grounding_score(payload: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    span_ids = {span["span_id"] for span in payload.get("spans", [])}
    references = 0
    valid_references = 0

    for claim in payload.get("claims", []):
        for span_id in claim.get("source_span_ids", []):
            references += 1
            if span_id in span_ids:
                valid_references += 1
            else:
                notes.append(f"Claim references unknown span id: {span_id}")
    for evidence_item in payload.get("evidence_items", []):
        for span_id in evidence_item.get("source_span_ids", []):
            references += 1
            if span_id in span_ids:
                valid_references += 1
            else:
                notes.append(
                    f"EvidenceItem {evidence_item.get('evidence_id', '<unknown>')} references unknown span id: {span_id}"
                )

    if references == 0:
        return 0.0, ["No span grounding references were found."]
    return valid_references / references, notes


def relation_score(payload: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    scored = 0
    valid = 0

    for evidence_item in payload.get("evidence_items", []):
        role = evidence_item.get("role")
        scored += 1
        if role in VALID_RELATIONS:
            valid += 1
        else:
            notes.append(
                f"EvidenceItem {evidence_item.get('evidence_id', '<unknown>')} uses invalid role: {role}"
            )

    for link in payload.get("claim_evidence_links", []):
        relation = link.get("relation")
        scored += 1
        if relation in VALID_RELATIONS:
            valid += 1
        else:
            notes.append(
                f"ClaimEvidenceLink {link.get('link_id', '<unknown>')} uses invalid relation: {relation}"
            )
    if scored == 0:
        return 0.0, ["No relations were found."]
    return valid / scored, notes


def ontology_score(payload: dict[str, Any]) -> float:
    claims = payload.get("claims", [])
    if not claims:
        return 0.0

    fields_seen = 0
    fields_mapped = 0
    for claim in claims:
        for field_name in ["subject", "predicate", "object"]:
            field = claim.get(field_name, {})
            fields_seen += 1
            ontology = field.get("ontology")
            if isinstance(ontology, dict) and ontology.get("selected_mapping"):
                fields_mapped += 1
    if fields_seen == 0:
        return 0.0
    return fields_mapped / fields_seen


def total_score(payload: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    schema_errors = validate_against_schema(payload, "extraction.schema.json")
    schema_validity = 0.0 if schema_errors else 1.0
    structure = structure_score(payload)
    grounding, grounding_notes = grounding_score(payload)
    relations, relation_notes = relation_score(payload)
    ontology = ontology_score(payload)

    total = (
        0.35 * schema_validity
        + 0.15 * structure
        + 0.30 * grounding
        + 0.10 * relations
        + 0.10 * ontology
    )

    notes = []
    notes.extend(schema_errors)
    notes.extend(grounding_notes)
    notes.extend(relation_notes)
    if not notes:
        notes.append("Payload passed the demo validator checks.")

    return {
        "schema_validity": round(schema_validity, 3),
        "structure": round(structure, 3),
        "grounding": round(grounding, 3),
        "relations": round(relations, 3),
        "ontology": round(ontology, 3),
        "score_total": round(total, 3),
    }, notes


def _require_fields(payload: dict[str, Any], required: list[str], label: str) -> list[str]:
    errors: list[str] = []
    for field in required:
        if field not in payload:
            errors.append(f"{label} is missing required field: {field}")
    return errors


def _validate_paper(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Paper payload must be an object."]
    errors = _require_fields(payload, ["paper_id", "title", "authors", "year"], "Paper")
    if "authors" in payload and not isinstance(payload["authors"], list):
        errors.append("Paper.authors must be a list.")
    if "year" in payload and not isinstance(payload["year"], int):
        errors.append("Paper.year must be an integer.")
    return errors


def _validate_span(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Span payload must be an object."]
    errors = _require_fields(payload, ["span_id", "paper_id", "text"], "Span")
    for field in ["span_id", "paper_id", "text"]:
        if field in payload and not isinstance(payload[field], str):
            errors.append(f"Span.{field} must be a string.")
    return errors


def _validate_semantic_field(payload: dict[str, Any], label: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{label} must be an object."]
    if "value" not in payload or not isinstance(payload["value"], str):
        return [f"{label}.value must be a string."]
    if "entity_type" in payload and not isinstance(payload["entity_type"], str):
        return [f"{label}.entity_type must be a string."]
    return []


def _validate_context_map(payload: dict[str, Any], label: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{label} must be an object."]
    errors: list[str] = []
    for key, value in payload.items():
        errors.extend(_validate_semantic_field(value, f"{label}.{key}"))
    return errors


def _validate_claim(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Claim payload must be an object."]
    errors = _require_fields(
        payload,
        [
            "claim_id",
            "paper_id",
            "claim_text",
            "subject",
            "predicate",
            "object",
            "claim_kind",
            "epistemic_status",
            "support_origin",
            "source_span_ids",
        ],
        "Claim",
    )
    if "subject" in payload:
        errors.extend(_validate_semantic_field(payload["subject"], "Claim.subject"))
    if "predicate" in payload:
        errors.extend(_validate_semantic_field(payload["predicate"], "Claim.predicate"))
    if "object" in payload:
        errors.extend(_validate_semantic_field(payload["object"], "Claim.object"))
    if "source_span_ids" in payload and not isinstance(payload["source_span_ids"], list):
        errors.append("Claim.source_span_ids must be a list.")
    if "context" in payload:
        errors.extend(_validate_context_map(payload["context"], "Claim.context"))
    if "details" in payload and not isinstance(payload["details"], dict):
        errors.append("Claim.details must be an object.")
    return errors


def _validate_evidence_item(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["EvidenceItem payload must be an object."]
    errors = _require_fields(
        payload,
        ["evidence_id", "paper_id", "role", "summary_text", "evidence_method", "source_span_ids"],
        "EvidenceItem",
    )
    if payload.get("role") not in VALID_RELATIONS:
        errors.append("EvidenceItem.role must be one of the supported relation labels.")
    if "evidence_method" in payload:
        errors.extend(_validate_semantic_field(payload["evidence_method"], "EvidenceItem.evidence_method"))
    if "outcome_type" in payload and payload["outcome_type"] is not None:
        errors.extend(_validate_semantic_field(payload["outcome_type"], "EvidenceItem.outcome_type"))
    if "presentation_type" in payload and payload["presentation_type"] is not None:
        errors.extend(_validate_semantic_field(payload["presentation_type"], "EvidenceItem.presentation_type"))
    if "source_span_ids" in payload and not isinstance(payload["source_span_ids"], list):
        errors.append("EvidenceItem.source_span_ids must be a list.")
    if "context" in payload:
        errors.extend(_validate_context_map(payload["context"], "EvidenceItem.context"))
    if "details" in payload and not isinstance(payload["details"], dict):
        errors.append("EvidenceItem.details must be an object.")
    return errors


def _validate_claim_evidence_link(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["ClaimEvidenceLink payload must be an object."]
    errors = _require_fields(payload, ["link_id", "claim_id", "evidence_id", "relation"], "ClaimEvidenceLink")
    if payload.get("relation") not in VALID_RELATIONS:
        errors.append("ClaimEvidenceLink.relation must be one of the supported relation labels.")
    return errors


def _validate_extraction(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Extraction payload must be an object."]
    errors = _require_fields(
        payload,
        ["paper", "spans", "claims", "evidence_items", "claim_evidence_links"],
        "Extraction",
    )
    if "paper" in payload:
        errors.extend(_validate_paper(payload["paper"]))
    if "spans" in payload:
        if not isinstance(payload["spans"], list) or not payload["spans"]:
            errors.append("Extraction.spans must be a non-empty list.")
        else:
            for span in payload["spans"]:
                errors.extend(_validate_span(span))
    if "claims" in payload:
        if not isinstance(payload["claims"], list) or not payload["claims"]:
            errors.append("Extraction.claims must be a non-empty list.")
        else:
            for claim in payload["claims"]:
                errors.extend(_validate_claim(claim))
    if "evidence_items" in payload:
        if not isinstance(payload["evidence_items"], list) or not payload["evidence_items"]:
            errors.append("Extraction.evidence_items must be a non-empty list.")
        else:
            for evidence_item in payload["evidence_items"]:
                errors.extend(_validate_evidence_item(evidence_item))
    if "claim_evidence_links" in payload:
        if not isinstance(payload["claim_evidence_links"], list) or not payload["claim_evidence_links"]:
            errors.append("Extraction.claim_evidence_links must be a non-empty list.")
        else:
            for link in payload["claim_evidence_links"]:
                errors.extend(_validate_claim_evidence_link(link))
    return errors


def _validate_validator_score(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["ValidatorScore payload must be an object."]
    errors = _require_fields(
        payload,
        ["paper_id", "validator_id", "accepted", "score_total", "score_components", "notes"],
        "ValidatorScore",
    )
    components = payload.get("score_components")
    if not isinstance(components, dict):
        errors.append("ValidatorScore.score_components must be an object.")
    else:
        errors.extend(
            _require_fields(
                components,
                ["schema_validity", "structure", "grounding", "relations", "ontology"],
                "ValidatorScore.score_components",
            )
        )
    if "notes" in payload and not isinstance(payload["notes"], list):
        errors.append("ValidatorScore.notes must be a list.")
    return errors
