from __future__ import annotations

from dataclasses import dataclass
from typing import Any


JUDGE_V1_DECISIONS = ("accept", "revise", "reject")

JUDGE_V1_DIAGNOSTIC_KEYS = (
    "is_meaningful_claim",
    "claim_text_faithful",
    "claim_is_self_consistent",
    "local_context_sufficient",
    "context_supported_elsewhere_in_paper",
    "supporting_evidence_present_somewhere",
    "evidence_links_complete",
    "spo_graph_compatible",
)

JUDGE_V1_DIMENSION_KEYS = (
    "claim_target_selection",
    "claim_faithfulness",
    "local_context_capture",
    "paper_context_alignment",
    "details_quality",
    "spo_graph_quality",
    "evidence_support_presence",
    "evidence_linking_completeness",
)


@dataclass(frozen=True)
class JudgeV1DimensionSpec:
    key: str
    label: str
    weight: float
    description: str
    accept_guidance: str
    revise_guidance: str
    reject_guidance: str
    notes: tuple[str, ...] = ()


JUDGE_V1_DIMENSIONS: tuple[JudgeV1DimensionSpec, ...] = (
    JudgeV1DimensionSpec(
        key="claim_target_selection",
        label="Claim Target Selection",
        weight=0.18,
        description=(
            "Does the extraction target a substantive paper-specific claim, result, interpretation, or comparison "
            "worth keeping, rather than prose scaffolding, citation chatter, or generic background?"
        ),
        accept_guidance="The extracted item is clearly a meaningful scientific claim or result for this paper.",
        revise_guidance="The item is claim-like but not the strongest or cleanest target in context.",
        reject_guidance="The extracted item is not really an atomic claim/result target for this schema.",
    ),
    JudgeV1DimensionSpec(
        key="claim_faithfulness",
        label="Claim Faithfulness",
        weight=0.20,
        description=(
            "Is the claim text faithful to the paper, scientifically non-misleading, and internally coherent even if "
            "some qualifiers are stored elsewhere?"
        ),
        accept_guidance="The claim text is faithful and does not overstate what the paper says.",
        revise_guidance="The claim is basically right but compressed, slightly underqualified, or awkwardly phrased.",
        reject_guidance="The claim text materially misstates the paper or creates a misleading meaning.",
        notes=("This dimension should separate claim correctness from evidence-link completeness.",),
    ),
    JudgeV1DimensionSpec(
        key="local_context_capture",
        label="Local Context Capture",
        weight=0.14,
        description=(
            "Within the claim object itself, are important qualifiers, conditions, subgroup restrictions, thresholds, "
            "modalities, and comparison cues preserved somewhere appropriate across claim text, context, or details?"
        ),
        accept_guidance="The locally stored claim object preserves the key scientific qualifiers needed to read it safely.",
        revise_guidance="The main relation is present but some qualifiers or conditions should be added locally.",
        reject_guidance="The local claim object omits essential context so badly that the claim becomes misleading.",
    ),
    JudgeV1DimensionSpec(
        key="paper_context_alignment",
        label="Paper Context Alignment",
        weight=0.16,
        description=(
            "If the claim is not fully self-contained, is the missing context or support clearly recoverable elsewhere "
            "in the paper-level extraction packet, such as paper summary, peer claims, or evidence registry?"
        ),
        accept_guidance="Paper-level context clearly supports the interpretation and resolves any local compression.",
        revise_guidance="The claim appears valid, but the paper-level context should be linked or surfaced more explicitly.",
        reject_guidance="Neither local nor paper-level context makes the claim scientifically defensible.",
        notes=(
            "Do not punish an atomic claim merely because not every qualifier fits inside one sentence.",
            "This dimension exists to stop false missing-context judgments when context is distributed elsewhere in the paper.",
        ),
    ),
    JudgeV1DimensionSpec(
        key="details_quality",
        label="Details Quality",
        weight=0.10,
        description=(
            "Are effect sizes, cohort names, thresholds, support origin, and other structured details placed sensibly "
            "in context/details rather than being dropped or jammed into the SPO core?"
        ),
        accept_guidance="Structured details strengthen fidelity without bloating the core SPO.",
        revise_guidance="Some useful details are present but incomplete, weakly placed, or inconsistently structured.",
        reject_guidance="Important structured detail is absent or misplaced enough to distort the claim.",
    ),
    JudgeV1DimensionSpec(
        key="spo_graph_quality",
        label="SPO Graph Quality",
        weight=0.12,
        description=(
            "Is the SPO an atomic, queryable graph projection with node-like subject/object and a sensible predicate, "
            "without pretending to carry all paper nuance?"
        ),
        accept_guidance="The SPO is graph-friendly and consistent with the richer claim object.",
        revise_guidance="The SPO mostly tracks the claim but has soft node boundaries or an underspecified predicate.",
        reject_guidance="The SPO is materially wrong, contradictory, or unusable as a graph projection.",
    ),
    JudgeV1DimensionSpec(
        key="evidence_support_presence",
        label="Evidence Support Presence",
        weight=0.06,
        description=(
            "Does the extraction packet indicate that relevant supporting evidence exists either locally or elsewhere in "
            "the paper, especially for results claims and discussion claims that point back to results?"
        ),
        accept_guidance="Relevant supporting evidence is clearly present in the extraction packet.",
        revise_guidance="The claim looks plausible, but evidence support should be surfaced more clearly.",
        reject_guidance="The claim appears unsupported, contradicted, or disconnected from any identifiable paper evidence.",
        notes=("Separate evidence presence from evidence-link completeness.",),
    ),
    JudgeV1DimensionSpec(
        key="evidence_linking_completeness",
        label="Evidence Linking Completeness",
        weight=0.04,
        description=(
            "When supporting evidence exists, are the evidence items and links connected to the claim clearly enough to "
            "preserve provenance and make the support inspectable?"
        ),
        accept_guidance="Evidence links are explicit and adequate.",
        revise_guidance="Support exists but the claim should be linked more explicitly to the right evidence item(s).",
        reject_guidance="The supplied links are clearly wrong or contradict the claim.",
        notes=("Missing or incomplete links should usually lead to revise, not reject, when the claim itself is faithful.",),
    ),
)

JUDGE_V1_DIMENSIONS_BY_KEY = {dimension.key: dimension for dimension in JUDGE_V1_DIMENSIONS}


def normalize_judge_v1_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parsed or {})
    diagnostics = normalized.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    dimension_scores = normalized.get("dimension_scores", {})
    if not isinstance(dimension_scores, dict):
        dimension_scores = {}
    dimension_reasons = normalized.get("dimension_reasons", {})
    if not isinstance(dimension_reasons, dict):
        dimension_reasons = {}
    normalized_dimension_scores, normalization_mode = _normalize_dimension_scores(dimension_scores)
    model_overall_score = _coerce_float(normalized.get("overall_score"))
    computed_overall_score = _weighted_overall_score(normalized_dimension_scores)
    normalized["diagnostics"] = diagnostics
    normalized["dimension_scores"] = normalized_dimension_scores
    normalized["dimension_reasons"] = dimension_reasons
    normalized["model_overall_score"] = "" if model_overall_score is None else round(model_overall_score, 6)
    normalized["score_normalization"] = normalization_mode
    if computed_overall_score is not None:
        normalized["overall_score"] = round(computed_overall_score, 6)
    elif model_overall_score is not None:
        normalized["overall_score"] = round(_clamp_01(model_overall_score), 6)
    return normalized


def flatten_judge_v1_payload(parsed: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_judge_v1_payload(parsed)
    diagnostics = normalized.get("diagnostics", {})
    dimension_scores = normalized.get("dimension_scores", {})
    dimension_reasons = normalized.get("dimension_reasons", {})

    flat = {
        "llm_judge_v1_decision": str(normalized.get("decision", "parse_error")).strip().lower() or "parse_error",
        "llm_judge_v1_overall_score": str(normalized.get("overall_score", "")),
        "llm_judge_v1_model_overall_score": str(normalized.get("model_overall_score", "")),
        "llm_judge_v1_score_normalization": str(normalized.get("score_normalization", "")),
        "llm_judge_v1_primary_failure": str(normalized.get("primary_failure", "")),
        "llm_judge_v1_secondary_failures": ", ".join(str(item) for item in normalized.get("secondary_failures", [])),
        "llm_judge_v1_error_tags": ", ".join(str(tag) for tag in normalized.get("error_tags", [])),
        "llm_judge_v1_context_location_assessment": str(normalized.get("context_location_assessment", "")),
        "llm_judge_v1_support_assessment": str(normalized.get("support_assessment", "")),
        "llm_judge_v1_missing_elements": ", ".join(str(item) for item in normalized.get("missing_elements", [])),
        "llm_judge_v1_feedback": str(normalized.get("feedback", "")),
    }
    for key in JUDGE_V1_DIAGNOSTIC_KEYS:
        value = diagnostics.get(key)
        flat[f"llm_judge_v1_{key}"] = "" if value is None else str(bool(value)).lower()
    for key in JUDGE_V1_DIMENSION_KEYS:
        flat[f"llm_judge_v1_{key}"] = str(dimension_scores.get(key, ""))
        flat[f"llm_judge_v1_{key}_reason"] = str(dimension_reasons.get(key, ""))
    return flat


def _normalize_dimension_scores(dimension_scores: dict[str, Any]) -> tuple[dict[str, Any], str]:
    numeric_scores: dict[str, float] = {}
    normalized_scores: dict[str, Any] = {}
    for key in JUDGE_V1_DIMENSION_KEYS:
        value = _coerce_float(dimension_scores.get(key))
        if value is None:
            normalized_scores[key] = ""
            continue
        numeric_scores[key] = value

    if _looks_like_weighted_contributions(numeric_scores):
        for key in JUDGE_V1_DIMENSION_KEYS:
            if key not in numeric_scores:
                continue
            weight = JUDGE_V1_DIMENSIONS_BY_KEY[key].weight
            normalized_scores[key] = round(_clamp_01(numeric_scores[key] / weight), 6) if weight > 0 else ""
        return normalized_scores, "weighted_contributions_to_raw"

    for key in JUDGE_V1_DIMENSION_KEYS:
        if key not in numeric_scores:
            continue
        normalized_scores[key] = round(_clamp_01(numeric_scores[key]), 6)
    return normalized_scores, "raw_scores"


def _looks_like_weighted_contributions(numeric_scores: dict[str, float]) -> bool:
    if not numeric_scores:
        return False
    if any(value < 0.0 or value > 1.0 for value in numeric_scores.values()):
        return False
    if any(value > JUDGE_V1_DIMENSIONS_BY_KEY[key].weight + 1e-9 for key, value in numeric_scores.items()):
        return False
    return sum(numeric_scores.values()) <= 1.0 + 1e-9


def _weighted_overall_score(dimension_scores: dict[str, Any]) -> float | None:
    weighted_sum = 0.0
    seen = False
    for dimension in JUDGE_V1_DIMENSIONS:
        value = _coerce_float(dimension_scores.get(dimension.key))
        if value is None:
            continue
        weighted_sum += _clamp_01(value) * dimension.weight
        seen = True
    if not seen:
        return None
    return _clamp_01(weighted_sum)


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _clamp_01(value: float) -> float:
    return max(0.0, min(1.0, value))
