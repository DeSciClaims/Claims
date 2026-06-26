from __future__ import annotations

import json
import re
from typing import Any


DEFAULT_CLAIM_PROFILE = "generic_result"

CLAIM_PROFILES: dict[str, dict[str, Any]] = {
    "generic_result": {
        "description": "Fallback profile for result claims when a more specific profile is not yet inferable.",
        "allowed_context_keys": [
            "population",
            "species",
            "setting",
            "timepoint",
            "comparator",
            "cohort",
            "condition",
            "modality",
            "threshold",
            "subset",
            "analysis_context",
            "phenotype_context",
            "intervention",
            "citation_context",
            "ancestry",
            "replication_stage",
            "subgroup",
            "mechanism",
            "equilibrium_scope",
            "source_scope",
            "core_qualifier",
            "peripheral_qualifier",
        ],
        "allowed_details_keys": [
            "effect_size",
            "odds_ratio",
            "effect_direction",
            "statistical_significance",
            "count",
            "sample_size",
            "study_count",
            "model_type",
            "confidence_qualifier",
            "directionality_note",
            "subject_role",
            "object_role",
            "estimator",
            "p_value",
            "ci_low",
            "ci_high",
            "variance_explained",
            "upper_bound",
            "standard_error",
            "lag",
            "constraint_note",
            "outcome_name",
            "unit",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": [],
        "forbidden_details_keys": [],
        "semantic_invariants": [
            "Preserve meaning-critical qualifiers in context or details, not only in claim_text.",
            "Do not put sample size, counts, proportions, p-values, confidence intervals, or effect sizes in ontology-target fields.",
        ],
    },
    "gwas_association_result": {
        "description": "A specific variant, locus, SNP, genotype feature, or gene is associated with a phenotype or outcome. Use polygenic_score_result instead for aggregate polygenic scores or variance explained by many SNPs.",
        "allowed_context_keys": [
            "population",
            "cohort",
            "ancestry",
            "setting",
            "phenotype_context",
            "replication_stage",
            "subgroup",
            "condition",
            "modality",
        ],
        "allowed_details_keys": [
            "variant_id",
            "locus",
            "gene_symbol",
            "outcome_name",
            "estimator",
            "effect_size",
            "odds_ratio",
            "effect_direction",
            "p_value",
            "ci_low",
            "ci_high",
            "sample_size",
            "model_type",
            "variance_explained",
            "subject_role",
            "object_role",
            "directionality_note",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["sample_size"],
        "forbidden_details_keys": [],
        "semantic_invariants": [
            "If sign or direction is stated, preserve it in details.effect_direction or details.directionality_note.",
            "Preserve variant/trait role assignment; do not flip subject and object.",
            "For odds ratio claims, the object is the phenotype/outcome; details.odds_ratio stores the odds-ratio value.",
            "For variance-explained claims about a single variant, the object is the phenotype/trait whose variance is explained; details.variance_explained stores the numeric variance.",
            "Keep sample_size and p_value in details, never context.",
        ],
    },
    "regression_association_result": {
        "description": "A regression, correlation, association, or adjusted statistical estimate relating variables.",
        "allowed_context_keys": [
            "population",
            "cohort",
            "setting",
            "timepoint",
            "condition",
            "analysis_context",
            "subgroup",
            "modality",
        ],
        "allowed_details_keys": [
            "outcome_name",
            "estimator",
            "effect_size",
            "effect_direction",
            "unit",
            "p_value",
            "ci_low",
            "ci_high",
            "sample_size",
            "model_type",
            "lag",
            "subject_role",
            "object_role",
            "directionality_note",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["sample_size", "p_value", "effect_size"],
        "forbidden_details_keys": [],
        "semantic_invariants": [
            "If a coefficient, association, or relationship is positive or negative, preserve that sign explicitly.",
            "If one variable leads/precedes another, preserve temporal lag in details.lag.",
            "Preserve dependent/independent variable roles in details.subject_role and details.object_role when clear.",
        ],
    },
    "polygenic_score_result": {
        "description": "A polygenic score, risk score, or aggregate genetic score predicts or explains an outcome.",
        "allowed_context_keys": [
            "population",
            "cohort",
            "ancestry",
            "setting",
            "phenotype_context",
            "condition",
            "modality",
        ],
        "allowed_details_keys": [
            "score_type",
            "outcome_name",
            "effect_size",
            "effect_direction",
            "variance_explained",
            "upper_bound",
            "standard_error",
            "sample_size",
            "model_type",
            "p_value",
            "subject_role",
            "object_role",
            "directionality_note",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["sample_size", "variance_explained"],
        "forbidden_details_keys": [],
        "semantic_invariants": [
            "Preserve whether the score predicts, explains variance, stratifies risk, or controls for genetic factors.",
            "Keep variance explained and sample size in details.",
        ],
    },
    "intervention_effect_result": {
        "description": "An intervention, exposure, treatment, or policy changes an outcome.",
        "allowed_context_keys": [
            "population",
            "species",
            "dose",
            "timepoint",
            "comparator",
            "setting",
            "subgroup",
            "condition",
            "intervention",
            "modality",
        ],
        "allowed_details_keys": [
            "outcome_name",
            "estimator",
            "effect_size",
            "effect_direction",
            "unit",
            "p_value",
            "ci_low",
            "ci_high",
            "sample_size",
            "model_type",
            "directionality_note",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["sample_size", "effect_size"],
        "forbidden_details_keys": [],
        "semantic_invariants": [
            "Preserve intervention, comparator, outcome, and direction when stated.",
            "Do not upgrade modal or conditional effects into categorical effects.",
        ],
    },
    "meta_analysis_result": {
        "description": "A meta-analysis or systematic evidence synthesis reports an effect.",
        "allowed_context_keys": [
            "population",
            "setting",
            "comparator",
            "timepoint",
            "phenotype_context",
            "condition",
        ],
        "allowed_details_keys": [
            "outcome_name",
            "estimator",
            "effect_size",
            "effect_direction",
            "unit",
            "p_value",
            "ci_low",
            "ci_high",
            "study_count",
            "sample_size",
            "directionality_note",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["study_count", "sample_size", "effect_size"],
        "forbidden_details_keys": [],
        "semantic_invariants": [
            "Keep study_count, sample_size, and effect estimates in details.",
        ],
    },
    "mechanistic_claim": {
        "description": "A claim whose central meaning is a mechanism, pathway, mediator, or through-which relation.",
        "allowed_context_keys": [
            "population",
            "species",
            "setting",
            "condition",
            "mechanism",
            "phenotype_context",
            "modality",
        ],
        "allowed_details_keys": [
            "outcome_name",
            "directionality_note",
            "constraint_note",
            "subject_role",
            "object_role",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["sample_size", "p_value", "effect_size"],
        "forbidden_details_keys": ["sample_size", "p_value"],
        "semantic_invariants": [
            "If the source says a relation holds through a mechanism, preserve the mechanism in context.mechanism.",
            "Do not collapse mechanism into a generic association.",
        ],
    },
    "theoretical_proposition": {
        "description": "A conceptual, equilibrium, source-scope, or mathematical proposition.",
        "allowed_context_keys": [
            "setting",
            "condition",
            "timepoint",
            "subset",
            "mechanism",
            "modality",
            "equilibrium_scope",
            "source_scope",
            "core_qualifier",
            "peripheral_qualifier",
        ],
        "allowed_details_keys": [
            "constraint_note",
            "model_type",
            "lag",
            "subject_role",
            "object_role",
        ],
        "required_context_keys": [],
        "required_details_keys": [],
        "forbidden_context_keys": ["sample_size", "p_value", "effect_size"],
        "forbidden_details_keys": ["sample_size", "p_value", "effect_size"],
        "semantic_invariants": [
            "Preserve equilibrium/source/scope qualifiers when they limit the proposition.",
            "Do not turn coordinated equilibrium propositions into malformed binary SPO relations.",
            "Preserve modal qualifiers such as can/may/must/conditional.",
        ],
    },
}

FIELD_POLICIES: dict[str, Any] = {
    "claim_field_roles": {
        "claim.subject": "ontology-target",
        "claim.predicate": "ontology-target",
        "claim.object": "ontology-target",
        "claim.context.population": "ontology-target",
        "claim.context.species": "ontology-target",
        "claim.context.setting": "ontology-target",
        "claim.context.timepoint": "prose-qualifier",
        "claim.context.comparator": "ontology-target",
        "claim.context.cohort": "prose-qualifier",
        "claim.context.condition": "prose-qualifier",
        "claim.context.dose": "structured-payload",
        "claim.context.modality": "prose-qualifier",
        "claim.context.threshold": "prose-qualifier",
        "claim.context.subset": "prose-qualifier",
        "claim.context.analysis_context": "ontology-target",
        "claim.context.phenotype_context": "ontology-target",
        "claim.context.intervention": "ontology-target",
        "claim.context.citation_context": "prose-qualifier",
        "claim.context.ancestry": "ontology-target",
        "claim.context.replication_stage": "prose-qualifier",
        "claim.context.subgroup": "prose-qualifier",
        "claim.context.mechanism": "prose-qualifier",
        "claim.context.equilibrium_scope": "prose-qualifier",
        "claim.context.source_scope": "prose-qualifier",
        "claim.context.core_qualifier": "prose-qualifier",
        "claim.context.peripheral_qualifier": "prose-qualifier",
        "claim.details.effect_size": "structured-payload",
        "claim.details.odds_ratio": "structured-payload",
        "claim.details.effect_direction": "structured-payload",
        "claim.details.statistical_significance": "structured-payload",
        "claim.details.count": "structured-payload",
        "claim.details.sample_size": "structured-payload",
        "claim.details.study_count": "structured-payload",
        "claim.details.model_type": "ontology-target",
        "claim.details.confidence_qualifier": "prose-qualifier",
        "claim.details.directionality_note": "prose-qualifier",
        "claim.details.subject_role": "prose-qualifier",
        "claim.details.object_role": "prose-qualifier",
        "claim.details.estimator": "ontology-target",
        "claim.details.p_value": "structured-payload",
        "claim.details.ci_low": "structured-payload",
        "claim.details.ci_high": "structured-payload",
        "claim.details.variance_explained": "structured-payload",
        "claim.details.upper_bound": "structured-payload",
        "claim.details.standard_error": "structured-payload",
        "claim.details.lag": "structured-payload",
        "claim.details.constraint_note": "prose-qualifier",
        "claim.details.outcome_name": "ontology-target",
        "claim.details.unit": "structured-payload",
        "claim.details.variant_id": "structured-payload",
        "claim.details.locus": "ontology-target",
        "claim.details.gene_symbol": "ontology-target",
        "claim.details.score_type": "ontology-target",
    },
    "profile_field_role_overrides": {
        "theoretical_proposition": {
            "claim.context.setting": "prose-qualifier",
            "claim.object": "prose-qualifier",
        }
    },
    "evidence_field_roles": {
        "evidence.evidence_method": "ontology-target",
        "evidence.outcome_type": "ontology-target",
        "evidence.presentation_type": "ontology-target",
        "evidence.context.population": "ontology-target",
        "evidence.context.species": "ontology-target",
        "evidence.context.setting": "ontology-target",
        "evidence.context.timepoint": "prose-qualifier",
        "evidence.context.condition": "prose-qualifier",
        "evidence.context.cohort": "prose-qualifier",
        "evidence.context.analysis_context": "ontology-target",
        "evidence.context.comparator": "ontology-target",
        "evidence.context.citation_context": "prose-qualifier",
        "evidence.context.dose": "structured-payload",
        "evidence.context.phenotype_context": "ontology-target",
        "evidence.context.section_type": "prose-qualifier",
        "evidence.context.equilibrium_scope": "prose-qualifier",
        "evidence.context.source_scope": "prose-qualifier",
        "evidence.details.outcome_name": "ontology-target",
        "evidence.details.value": "structured-payload",
        "evidence.details.unit": "structured-payload",
        "evidence.details.sample_size": "structured-payload",
        "evidence.details.observation_type": "prose-qualifier",
        "evidence.details.measurement_type": "ontology-target",
        "evidence.details.estimator": "ontology-target",
        "evidence.details.effect_size": "structured-payload",
        "evidence.details.effect_direction": "structured-payload",
        "evidence.details.model_type": "ontology-target",
        "evidence.details.p_value": "structured-payload",
        "evidence.details.ci_low": "structured-payload",
        "evidence.details.ci_high": "structured-payload",
        "evidence.details.study_count": "structured-payload",
        "evidence.details.lag": "structured-payload",
    },
}

PROFILE_VALIDATION_RULES: dict[str, dict[str, Any]] = {
    "gwas_association_result": {
        "subject_value_patterns": [r"\brs\d+\b"],
        "subject_entity_type_terms": ["locus", "gene"],
        "object_entity_type_terms": ["phenotype", "trait", "outcome", "education", "educational", "attainment", "college", "completion"],
        "generic_subject_terms": ["snps", "all measured snps", "polygenic score", "genetic score"],
        "statistic_object_terms": ["odds ratio", "p value", "p-value", "effect size", "variance explained"],
    },
    "polygenic_score_result": {
        "subject_entity_type_terms": ["score", "polygenic", "genetic"],
        "object_entity_type_terms": ["phenotype", "trait", "outcome", "variance", "education", "educational", "attainment"],
        "statistic_object_terms": ["p value", "p-value", "effect size", "variance explained"],
    },
}

EVIDENCE_METHOD_PROFILES = {
    "observation": {
        "description": "Descriptive observational evidence without strong causal design.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint", "condition", "cohort"],
        "allowed_details_keys": ["outcome_name", "value", "unit", "sample_size", "observation_type", "measurement_type", "estimator"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "regression_estimate": {
        "description": "Association or adjusted estimate from a regression model.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint", "cohort", "condition", "analysis_context"],
        "allowed_details_keys": ["outcome_name", "effect_size", "effect_direction", "unit", "ci_low", "ci_high", "p_value", "sample_size", "model_type", "estimator", "lag"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "correlation_estimate": {
        "description": "Correlational relationship estimate without a causal claim.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint", "cohort", "condition"],
        "allowed_details_keys": ["outcome_name", "correlation_value", "effect_direction", "p_value", "sample_size", "lag"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "meta_analysis": {
        "description": "Evidence aggregated across multiple studies.",
        "allowed_context_keys": ["population", "species", "setting", "phenotype_context", "condition"],
        "allowed_details_keys": ["outcome_name", "effect_size", "effect_direction", "unit", "ci_low", "ci_high", "p_value", "study_count", "sample_size", "measurement_type"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "literature_review": {
        "description": "Evidence summarized from prior literature without a new quantitative aggregation.",
        "allowed_context_keys": ["population", "species", "setting", "citation_context", "condition"],
        "allowed_details_keys": ["outcome_name", "review_scope", "study_count"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "theoretical_argument": {
        "description": "Evidence derived from conceptual or theoretical reasoning.",
        "allowed_context_keys": ["section_type", "setting", "condition", "equilibrium_scope", "source_scope"],
        "allowed_details_keys": ["argument_type", "assumptions", "constraint_note", "outcome_name", "value", "effect_direction"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "derivation": {
        "description": "Evidence derived from formal or mathematical derivation.",
        "allowed_context_keys": ["section_type", "setting", "condition"],
        "allowed_details_keys": ["derivation_kind", "equation_refs", "assumptions", "constraint_note", "outcome_name", "value", "effect_direction"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "randomized_controlled_trial": {
        "description": "Experimental causal evidence from randomized assignment.",
        "allowed_context_keys": ["population", "species", "comparator", "dose", "timepoint", "setting"],
        "allowed_details_keys": ["outcome_name", "effect_size", "effect_direction", "unit", "ci_low", "ci_high", "p_value", "sample_size"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
    "quasi_experimental_estimate": {
        "description": "Causal estimate without randomization.",
        "allowed_context_keys": ["population", "setting", "timepoint", "comparator"],
        "allowed_details_keys": ["outcome_name", "effect_size", "effect_direction", "unit", "ci_low", "ci_high", "p_value", "sample_size", "estimator"],
        "required_context_keys": [],
        "required_details_keys": [],
    },
}

OUTCOME_TYPE_PROFILES = {
    "clinical_outcome": "Observed clinical endpoint, symptom score, or patient outcome.",
    "behavioral_outcome": "Observed behavior, cognition, or task performance.",
    "molecular_binding": "Binding affinity, receptor activation, or related molecular readout.",
    "gene_expression": "Expression-level measurement for genes or proteins.",
    "physiological_measure": "Physiological or biomarker measurement.",
    "phenotype": "Observed phenotype or trait.",
    "adverse_event": "Observed adverse event or safety outcome.",
    "mechanistic_pathway": "Mechanistic process or pathway-related outcome.",
    "structural_measure": "Structural or anatomical measure.",
    "quantitative_measure": "Generic quantitative observation when a finer outcome type is unclear.",
}

PRESENTATION_TYPE_PROFILES = {
    "text": "Evidence described in narrative prose.",
    "table": "Evidence primarily presented in a table.",
    "figure": "Evidence primarily presented in a figure or chart.",
    "caption": "Evidence primarily presented in a caption.",
    "supplement": "Evidence primarily presented in supplementary material.",
}

CLAIM_PROFILE = {
    "allowed_context_keys": sorted({key for profile in CLAIM_PROFILES.values() for key in profile["allowed_context_keys"]}),
    "allowed_details_keys": sorted({key for profile in CLAIM_PROFILES.values() for key in profile["allowed_details_keys"]}),
}


def normalize_claim_profile(value: Any) -> str:
    profile = str(value or "").strip()
    return profile if profile in CLAIM_PROFILES else DEFAULT_CLAIM_PROFILE


def infer_claim_profile(*, claim_text: str, subject: Any = None, predicate: Any = None, object_: Any = None, details: Any = None, context: Any = None) -> str:
    text = " ".join(
        str(part or "")
        for part in [
            claim_text,
            subject.get("value") if isinstance(subject, dict) else subject,
            predicate.get("value") if isinstance(predicate, dict) else predicate,
            object_.get("value") if isinstance(object_, dict) else object_,
        ]
    ).lower()
    raw_details = details if isinstance(details, dict) else {}
    raw_context = context if isinstance(context, dict) else {}

    if any(token in text for token in ("polygenic score", "genetic score", "all measured snps", "linear score")):
        return "polygenic_score_result"
    if any(token in text for token in ("snp", "locus", "variant", "genome-wide")):
        return "gwas_association_result"
    if any(token in text for token in ("regression", "coefficient", "associated", "association", "correlat", "odds ratio")):
        return "regression_association_result"
    if any(token in text for token in ("intervention", "treatment", "exposure", "policy", "reduced", "increased", "decreased")):
        return "intervention_effect_result"
    if any(token in text for token in ("meta-analysis", "meta analysis", "systematic review")):
        return "meta_analysis_result"
    if any(token in text for token in ("through", "mediated", "mechanism", "pathway")) or "mechanism" in raw_context:
        return "mechanistic_claim"
    if any(token in text for token in ("equilibrium", "theorem", "proof", "proposition", "model implies")):
        return "theoretical_proposition"
    if any(key in raw_details for key in ("p_value", "ci_low", "ci_high", "estimator")):
        return "regression_association_result"
    return DEFAULT_CLAIM_PROFILE


def claim_profile_prompt_json() -> str:
    return json.dumps(
        {
            "default_claim_profile": DEFAULT_CLAIM_PROFILE,
            "profiles": CLAIM_PROFILES,
            "field_roles": FIELD_POLICIES["claim_field_roles"],
            "profile_field_role_overrides": FIELD_POLICIES.get("profile_field_role_overrides", {}),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def evidence_method_prompt_json() -> str:
    return json.dumps(
        {"methods": EVIDENCE_METHOD_PROFILES},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def validate_claim_against_profile(claim: dict[str, Any]) -> list[str]:
    profile_name = normalize_claim_profile(claim.get("claim_profile"))
    profile = CLAIM_PROFILES.get(profile_name, CLAIM_PROFILES[DEFAULT_CLAIM_PROFILE])
    errors: list[str] = []

    for key in ("subject", "predicate", "object"):
        if not _semantic_value(claim.get(key)):
            errors.append(f"missing_{key}")

    context = claim.get("context") if isinstance(claim.get("context"), dict) else {}
    details = claim.get("details") if isinstance(claim.get("details"), dict) else {}
    allowed_context = set(profile.get("allowed_context_keys", []))
    allowed_details = set(profile.get("allowed_details_keys", []))
    forbidden_context = set(profile.get("forbidden_context_keys", []))
    forbidden_details = set(profile.get("forbidden_details_keys", []))

    for key in context:
        if key in forbidden_context:
            errors.append(f"forbidden_context.{key}")
        elif allowed_context and key not in allowed_context:
            errors.append(f"unexpected_context.{key}")
    for key in details:
        if key in forbidden_details:
            errors.append(f"forbidden_details.{key}")
        elif allowed_details and key not in allowed_details:
            errors.append(f"unexpected_details.{key}")

    for path, role in FIELD_POLICIES.get("claim_field_roles", {}).items():
        if role != "ontology-target":
            continue
        value = _value_at_path(claim, path.removeprefix("claim."))
        if _looks_like_statistical_payload(_semantic_value(value)):
            errors.append(f"statistical_payload_in_ontology_target.{path}")

    errors.extend(_validate_profile_semantics(profile_name, claim))
    return errors


def _validate_profile_semantics(profile_name: str, claim: dict[str, Any]) -> list[str]:
    rules = PROFILE_VALIDATION_RULES.get(profile_name)
    if not rules:
        return []

    errors: list[str] = []
    subject = claim.get("subject")
    object_ = claim.get("object")
    subject_value = _semantic_value(subject).lower()
    object_value = _semantic_value(object_).lower()
    subject_semantics = _semantic_search_text(subject).lower()
    object_semantics = _semantic_search_text(object_).lower()

    subject_terms = [str(term).lower() for term in rules.get("subject_entity_type_terms", [])]
    subject_patterns = [str(pattern) for pattern in rules.get("subject_value_patterns", [])]
    generic_subject_terms = [str(term).lower() for term in rules.get("generic_subject_terms", [])]
    if any(term == subject_value or term in subject_value for term in generic_subject_terms):
        errors.append(f"{profile_name}.generic_subject_requires_different_profile")
    if subject_terms or subject_patterns:
        subject_matches_term = any(term in subject_semantics for term in subject_terms)
        subject_matches_pattern = any(re.search(pattern, subject_value, re.IGNORECASE) for pattern in subject_patterns)
        if not (subject_matches_term or subject_matches_pattern):
            errors.append(f"{profile_name}.subject_role_mismatch")

    object_terms = [str(term).lower() for term in rules.get("object_entity_type_terms", [])]
    if object_terms and not any(term in object_semantics for term in object_terms):
        errors.append(f"{profile_name}.object_role_mismatch")

    statistic_terms = [str(term).lower() for term in rules.get("statistic_object_terms", [])]
    if any(term in object_value for term in statistic_terms) or _looks_like_statistical_payload(object_value):
        errors.append(f"{profile_name}.object_contains_structured_payload")

    return errors


def _semantic_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value", "")).strip()
    return str(value or "").strip()


def _semantic_entity_type(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("entity_type", "")).strip()
    return ""


def _semantic_search_text(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "").strip()
    ontology = value.get("ontology")
    if isinstance(ontology, dict):
        ontology_text = " ".join(str(raw or "") for raw in ontology.values())
    else:
        ontology_text = str(ontology or "")
    return " ".join(
        str(part or "").strip()
        for part in (value.get("value"), value.get("entity_type"), ontology_text)
        if str(part or "").strip()
    )


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _looks_like_statistical_payload(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if any(token in text for token in ("p =", "p-value", "p value", "odds ratio", "confidence interval")):
        return True
    return bool(re.fullmatch(r"[<>]?\s*\d+(?:\.\d+)?\s*(?:%|e-?\d+|×\s*10\s*-?\d+)?", text))
