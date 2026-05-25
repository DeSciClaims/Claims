from __future__ import annotations

from typing import Any


VALID_RELATIONS = {
    "supports",
    "relates_to",
    "contradicts",
    "qualifies",
    "refines",
    "replicates",
    "fails_to_replicate",
}


def validate_against_schema(payload: dict[str, Any], schema_name: str) -> list[str]:
    validators = {
        "source.schema.json": _validate_source,
        "chunk.schema.json": _validate_chunk,
        "claimframe.schema.json": _validate_claimframe,
        "meta_assertion.schema.json": _validate_meta_assertion,
        "extraction.schema.json": _validate_extraction,
        "validator_score.schema.json": _validate_validator_score,
    }
    validator = validators.get(schema_name)
    if validator is None:
        return [f"Unsupported schema: {schema_name}"]
    return validator(payload)


def structure_score(payload: dict[str, Any]) -> float:
    claim_records = payload.get("claim_records", [])
    if not claim_records:
        return 0.0
    has_claims = all("claim" in record for record in claim_records)
    has_evidence = all(record.get("evidence") for record in claim_records)
    return 1.0 if has_claims and has_evidence else 0.5


def grounding_score(payload: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    chunk_ids = {chunk["chunk_id"] for chunk in payload.get("chunks", [])}
    references = 0
    valid_references = 0

    for record in payload.get("claim_records", []):
        claim = record.get("claim", {})
        for chunk_id in claim.get("source_chunk_ids", []):
            references += 1
            if chunk_id in chunk_ids:
                valid_references += 1
            else:
                notes.append(f"Claim references unknown chunk id: {chunk_id}")
        for evidence in record.get("evidence", []):
            for chunk_id in evidence.get("source_chunk_ids", []):
                references += 1
                if chunk_id in chunk_ids:
                    valid_references += 1
                else:
                    notes.append(
                        f"Evidence {evidence.get('evidence_id', '<unknown>')} references unknown chunk id: {chunk_id}"
                    )

    if references == 0:
        return 0.0, ["No chunk grounding references were found."]
    return valid_references / references, notes


def relation_score(payload: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    evidence_items = [
        evidence
        for record in payload.get("claim_records", [])
        for evidence in record.get("evidence", [])
    ]
    if not evidence_items:
        return 0.0, ["No evidence items were found."]

    valid = 0
    for evidence in evidence_items:
        relation = evidence.get("relation_to_claim")
        if relation in VALID_RELATIONS:
            valid += 1
        else:
            notes.append(
                f"Evidence {evidence.get('evidence_id', '<unknown>')} uses invalid relation: {relation}"
            )
    return valid / len(evidence_items), notes


def ontology_score(payload: dict[str, Any]) -> float:
    claim_records = payload.get("claim_records", [])
    if not claim_records:
        return 0.0

    fields_seen = 0
    fields_mapped = 0
    for record in claim_records:
        claim = record.get("claim", {})
        for field_name in ["subject", "predicate", "object"]:
            field = claim.get(field_name, {})
            fields_seen += 1
            if field.get("selected_mapping"):
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


def _validate_source(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Source payload must be an object."]
    errors = _require_fields(payload, ["source_id", "title", "authors", "year"], "Source")
    if "authors" in payload and not isinstance(payload["authors"], list):
        errors.append("Source.authors must be a list.")
    if "year" in payload and not isinstance(payload["year"], int):
        errors.append("Source.year must be an integer.")
    return errors


def _validate_chunk(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Chunk payload must be an object."]
    errors = _require_fields(payload, ["chunk_id", "source_id", "section", "text"], "Chunk")
    for field in ["chunk_id", "source_id", "section", "text"]:
        if field in payload and not isinstance(payload[field], str):
            errors.append(f"Chunk.{field} must be a string.")
    return errors


def _validate_semantic_field(payload: dict[str, Any], label: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{label} must be an object."]
    if "value" not in payload or not isinstance(payload["value"], str):
        return [f"{label}.value must be a string."]
    return []


def _validate_claimframe(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["ClaimFrame payload must be an object."]
    errors = _require_fields(
        payload,
        [
            "claim_id",
            "claim_text",
            "subject",
            "predicate",
            "object",
            "claim_type",
            "epistemic_status",
            "source_chunk_ids",
        ],
        "ClaimFrame",
    )
    if "subject" in payload:
        errors.extend(_validate_semantic_field(payload["subject"], "ClaimFrame.subject"))
    if "predicate" in payload:
        errors.extend(_validate_semantic_field(payload["predicate"], "ClaimFrame.predicate"))
    if "object" in payload:
        errors.extend(_validate_semantic_field(payload["object"], "ClaimFrame.object"))
    if "source_chunk_ids" in payload and not isinstance(payload["source_chunk_ids"], list):
        errors.append("ClaimFrame.source_chunk_ids must be a list.")
    return errors


def _validate_meta_assertion(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["MetaAssertion payload must be an object."]
    errors = _require_fields(
        payload,
        ["assertion_id", "target_claim_id", "assertion_type", "summary", "confidence"],
        "MetaAssertion",
    )
    confidence = payload.get("confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        errors.append("MetaAssertion.confidence must be numeric.")
    return errors


def _validate_evidence(payload: dict[str, Any], label: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{label} must be an object."]
    errors = _require_fields(
        payload,
        ["evidence_id", "summary_text", "relation_to_claim", "evidence_type", "source_chunk_ids"],
        label,
    )
    if payload.get("relation_to_claim") not in VALID_RELATIONS:
        errors.append(f"{label}.relation_to_claim must be one of the supported relation labels.")
    if "source_chunk_ids" in payload and not isinstance(payload["source_chunk_ids"], list):
        errors.append(f"{label}.source_chunk_ids must be a list.")
    return errors


def _validate_extraction(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["Extraction payload must be an object."]
    errors = _require_fields(
        payload,
        ["task_id", "schema_version", "miner_id", "source", "chunks", "claim_records"],
        "Extraction",
    )
    if "source" in payload:
        errors.extend(_validate_source(payload["source"]))
    if "chunks" in payload:
        if not isinstance(payload["chunks"], list) or not payload["chunks"]:
            errors.append("Extraction.chunks must be a non-empty list.")
        else:
            for chunk in payload["chunks"]:
                errors.extend(_validate_chunk(chunk))
    if "claim_records" in payload:
        if not isinstance(payload["claim_records"], list) or not payload["claim_records"]:
            errors.append("Extraction.claim_records must be a non-empty list.")
        else:
            for index, record in enumerate(payload["claim_records"]):
                if not isinstance(record, dict):
                    errors.append(f"Extraction.claim_records[{index}] must be an object.")
                    continue
                if "claim" not in record:
                    errors.append(f"Extraction.claim_records[{index}] is missing claim.")
                else:
                    errors.extend(_validate_claimframe(record["claim"]))
                evidence = record.get("evidence")
                if not isinstance(evidence, list) or not evidence:
                    errors.append(f"Extraction.claim_records[{index}].evidence must be a non-empty list.")
                else:
                    for evidence_index, item in enumerate(evidence):
                        errors.extend(
                            _validate_evidence(
                                item,
                                f"Extraction.claim_records[{index}].evidence[{evidence_index}]",
                            )
                        )
    if "meta_assertions" in payload:
        if not isinstance(payload["meta_assertions"], list):
            errors.append("Extraction.meta_assertions must be a list.")
        else:
            for item in payload["meta_assertions"]:
                errors.extend(_validate_meta_assertion(item))
    return errors


def _validate_validator_score(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["ValidatorScore payload must be an object."]
    errors = _require_fields(
        payload,
        ["task_id", "validator_id", "miner_id", "accepted", "score_total", "score_components", "notes"],
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
