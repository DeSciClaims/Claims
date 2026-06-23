from __future__ import annotations

import re

from .models import FieldNormalization


NUMERIC_ONLY_RE = re.compile(r"^\s*[-+]?[\d.,]+(?:\s*%|\s*sd|\s*se)?\s*$", re.IGNORECASE)
COUNT_PHRASE_RE = re.compile(r"^\s*[\d,]+\s+(?:individuals|participants|subjects|samples?)\s*$", re.IGNORECASE)


def normalize_field_value(
    *,
    raw_text: str,
    field_path: str,
    field_role: str,
    normalization_rule: str | None,
) -> FieldNormalization:
    text = " ".join(raw_text.strip().split())
    normalized = text
    notes: list[str] = []
    semantic_payload_type: str | None = None

    if not text:
        return FieldNormalization(
            raw_text=raw_text,
            normalized_text="",
            field_role=field_role,
            normalization_rule=normalization_rule,
            normalization_status="empty",
            should_attempt_mapping=False,
            skip_reason="empty_text",
        )

    if field_role != "ontology-target":
        normalized, notes, semantic_payload_type = _apply_rule(text, normalization_rule)
        return FieldNormalization(
            raw_text=raw_text,
            normalized_text=normalized,
            field_role=field_role,
            normalization_rule=normalization_rule,
            normalization_status="role_skipped",
            should_attempt_mapping=False,
            skip_reason=f"non_target_{field_role}",
            notes=notes,
            semantic_payload_type=semantic_payload_type,
        )

    normalized, notes, semantic_payload_type = _apply_rule(text, normalization_rule)
    normalized = _strip_terminal_punctuation(normalized)

    if not normalized:
        return FieldNormalization(
            raw_text=raw_text,
            normalized_text="",
            field_role=field_role,
            normalization_rule=normalization_rule,
            normalization_status="normalized_empty",
            should_attempt_mapping=False,
            skip_reason="normalized_empty",
            notes=notes,
            semantic_payload_type=semantic_payload_type,
        )

    if _looks_numeric_payload(normalized):
        return FieldNormalization(
            raw_text=raw_text,
            normalized_text=normalized,
            field_role=field_role,
            normalization_rule=normalization_rule,
            normalization_status="payload_detected",
            should_attempt_mapping=False,
            skip_reason="numeric_payload",
            notes=notes + ["value looks like numeric/statistical payload"],
            semantic_payload_type=semantic_payload_type or "numeric_payload",
        )

    if COUNT_PHRASE_RE.match(normalized):
        return FieldNormalization(
            raw_text=raw_text,
            normalized_text=normalized,
            field_role=field_role,
            normalization_rule=normalization_rule,
            normalization_status="payload_detected",
            should_attempt_mapping=False,
            skip_reason="count_payload",
            notes=notes + ["value looks like a cohort/count payload"],
            semantic_payload_type=semantic_payload_type or "count_payload",
        )

    return FieldNormalization(
        raw_text=raw_text,
        normalized_text=normalized,
        field_role=field_role,
        normalization_rule=normalization_rule,
        normalization_status="normalized" if normalized != text else "unchanged",
        should_attempt_mapping=True,
        notes=notes,
        semantic_payload_type=semantic_payload_type,
    )


def _apply_rule(text: str, rule: str | None) -> tuple[str, list[str], str | None]:
    if not rule:
        return text, [], None
    if rule == "population":
        return _normalize_population(text)
    if rule == "setting":
        return _normalize_setting(text)
    if rule == "cohort":
        return _normalize_cohort(text)
    if rule == "condition":
        return _normalize_condition(text)
    if rule == "ancestry":
        return _normalize_ancestry(text)
    if rule == "phenotype":
        return _normalize_phenotype(text)
    if rule == "model_type":
        return _normalize_model_type(text)
    if rule == "estimator":
        return _normalize_estimator(text)
    if rule == "evidence_method":
        return _normalize_evidence_method(text)
    if rule == "outcome_type":
        return _normalize_outcome_type(text)
    if rule == "presentation_type":
        return _normalize_presentation_type(text)
    if rule == "measurement_type":
        return _normalize_measurement_type(text)
    if rule == "predicate":
        return _normalize_predicate(text)
    if rule == "subject_object":
        return _normalize_subject_object(text)
    return text, [], None


def _normalize_population(text: str) -> tuple[str, list[str], str | None]:
    normalized = text
    notes: list[str] = []
    replacements = [
        (r"^participants in ", ""),
        (r"^individuals in ", ""),
        (r"^participants from ", ""),
        (r"^individuals from ", ""),
        (r"^subset of ", ""),
        (r"\bindividuals\b", "population"),
        (r"\bparticipants\b", "population"),
        (r"\bsample\b", "sample population"),
    ]
    for pattern, replacement in replacements:
        new = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        if new != normalized:
            normalized = new
            notes.append(f"applied population rule `{pattern}`")
    normalized = normalized.strip(" ,")
    if normalized.lower() == "replication sample":
        normalized = "replication population"
        notes.append("normalized replication sample to replication population")
    return normalized, notes, "population"


def _normalize_setting(text: str) -> tuple[str, list[str], str | None]:
    lowered = text.lower()
    notes: list[str] = []
    if "gwas" in lowered or "genome-wide" in lowered:
        return "genome-wide association study", ["collapsed GWAS variant phrase"], "setting"
    if "meta-analysis" in lowered or "meta analysis" in lowered:
        return "meta-analysis", ["collapsed meta-analysis phrase"], "setting"
    if "literature" in lowered and "gwas" in lowered:
        return "genome-wide association study literature", ["collapsed GWAS literature phrase"], "setting"
    return text, notes, "setting"


def _normalize_cohort(text: str) -> tuple[str, list[str], str | None]:
    return text, ["cohort retained as provenance/prose value"], "cohort"


def _normalize_condition(text: str) -> tuple[str, list[str], str | None]:
    normalized = text.strip()
    if normalized.lower().startswith("through "):
        return normalized, ["mechanistic qualifier retained as prose"], "mechanism"
    if normalized.lower().startswith("without "):
        return normalized, ["constraint qualifier retained as prose"], "constraint"
    if normalized.lower().startswith("under "):
        return normalized, ["scope qualifier retained as prose"], "scope"
    return normalized, [], "condition"


def _normalize_ancestry(text: str) -> tuple[str, list[str], str | None]:
    lowered = text.lower()
    if "caucasian" in lowered:
        return "European ancestry", ["normalized caucasian to European ancestry"], "ancestry"
    return text, [], "ancestry"


def _normalize_phenotype(text: str) -> tuple[str, list[str], str | None]:
    normalized = text
    notes: list[str] = []
    normalized = re.sub(r"^variance in ", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^phenotypic variance in ", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^approximately [\d.\-% ]+ of the variance in ", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^educational attainment outcomes$", "educational attainment", normalized, flags=re.IGNORECASE)
    normalized = normalized.strip(" .")
    if normalized != text:
        notes.append("distilled phenotype phrase")
    return normalized, notes, "phenotype"


def _normalize_model_type(text: str) -> tuple[str, list[str], str | None]:
    lowered = text.lower()
    if "linear regression" in lowered:
        return "linear regression", ["canonicalized model_type"], "model_type"
    if "logistic regression" in lowered:
        return "logistic regression", ["canonicalized model_type"], "model_type"
    return text, [], "model_type"


def _normalize_estimator(text: str) -> tuple[str, list[str], str | None]:
    lowered = text.lower()
    if "odds ratio" in lowered:
        return "odds ratio", ["canonicalized estimator"], "estimator"
    if "hazard ratio" in lowered:
        return "hazard ratio", ["canonicalized estimator"], "estimator"
    if "variance explained" in lowered:
        return "variance explained", ["canonicalized estimator"], "estimator"
    return text, [], "estimator"


def _normalize_evidence_method(text: str) -> tuple[str, list[str], str | None]:
    lowered = text.lower().strip()
    mapping = {
        "observation": "observation",
        "meta analysis": "meta_analysis",
        "meta-analysis": "meta_analysis",
        "regression estimate": "regression_estimate",
        "theoretical argument": "theoretical_argument",
    }
    if lowered in mapping:
        return mapping[lowered], ["canonicalized evidence_method"], "evidence_method"
    return text, [], "evidence_method"


def _normalize_outcome_type(text: str) -> tuple[str, list[str], str | None]:
    return text.lower().replace(" ", "_"), ["canonicalized outcome_type"], "outcome_type"


def _normalize_presentation_type(text: str) -> tuple[str, list[str], str | None]:
    return text.lower(), ["canonicalized presentation_type"], "presentation_type"


def _normalize_measurement_type(text: str) -> tuple[str, list[str], str | None]:
    return text.lower(), ["canonicalized measurement_type"], "measurement_type"


def _normalize_predicate(text: str) -> tuple[str, list[str], str | None]:
    normalized = text.lower().strip()
    replacements = {
        "is associated with": "associated with",
        "are associated with": "associated with",
        "has an odds ratio of": "has odds ratio",
        "explains": "explains",
        "accounts for": "accounts for",
    }
    return replacements.get(normalized, normalized), ["canonicalized predicate"], "predicate"


def _normalize_subject_object(text: str) -> tuple[str, list[str], str | None]:
    normalized = text.strip()
    normalized = re.sub(r"^the\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^snp\s+", "", normalized, flags=re.IGNORECASE) if normalized.lower().startswith("snp rs") else normalized
    normalized = normalized.strip(" .")
    notes = ["trimmed subject/object phrase"] if normalized != text.strip() else []
    return normalized, notes, "semantic_entity"


def _looks_numeric_payload(text: str) -> bool:
    lowered = text.lower()
    if NUMERIC_ONLY_RE.match(text):
        return True
    marker_patterns = [
        r"\bp\s*=",
        r"\bci\b",
        r"\bsd\b",
        r"\bse\b",
        r"\bpercentage points\b",
        r"\bper allele\b",
        r"\btimes larger\b",
    ]
    digit_ratio = sum(ch.isdigit() for ch in text) / max(1, len(text))
    return digit_ratio > 0.3 or any(re.search(pattern, lowered) for pattern in marker_patterns)


def _strip_terminal_punctuation(text: str) -> str:
    return text.strip().strip(",;:.")
