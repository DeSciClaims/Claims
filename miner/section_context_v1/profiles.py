CLAIM_PROFILE = {
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
    ],
    "allowed_details_keys": [
        "effect_size",
        "statistical_significance",
        "count",
        "sample_size",
        "study_count",
        "model_type",
        "confidence_qualifier",
        "directionality_note",
    ],
}

EVIDENCE_METHOD_PROFILES = {
    "randomized_controlled_trial": {
        "description": "Experimental causal evidence from randomized assignment.",
        "allowed_context_keys": ["population", "species", "comparator", "dose", "timepoint", "setting"],
        "allowed_details_keys": ["outcome_name", "effect_size", "unit", "ci_low", "ci_high", "p_value", "sample_size"],
    },
    "laboratory_experiment": {
        "description": "Controlled experimental evidence from lab assays or bench work.",
        "allowed_context_keys": ["species", "assay_type", "dose", "timepoint", "temperature", "ph", "setting"],
        "allowed_details_keys": ["measurement_type", "outcome_name", "value", "unit", "target", "ci_low", "ci_high", "sample_size"],
    },
    "field_experiment": {
        "description": "Experimental evidence from interventions in real-world settings.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint", "comparator"],
        "allowed_details_keys": ["outcome_name", "effect_size", "unit", "ci_low", "ci_high", "p_value", "sample_size"],
    },
    "quasi_experimental_estimate": {
        "description": "Causal estimate without randomization.",
        "allowed_context_keys": ["population", "setting", "timepoint", "comparator"],
        "allowed_details_keys": ["outcome_name", "effect_size", "unit", "ci_low", "ci_high", "p_value", "sample_size", "estimator"],
    },
    "regression_estimate": {
        "description": "Association or adjusted estimate from a regression model.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint"],
        "allowed_details_keys": ["outcome_name", "effect_size", "unit", "ci_low", "ci_high", "p_value", "sample_size", "model_type"],
    },
    "correlation_estimate": {
        "description": "Correlational relationship estimate without a causal claim.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint"],
        "allowed_details_keys": ["outcome_name", "correlation_value", "p_value", "sample_size"],
    },
    "observation": {
        "description": "Descriptive observational evidence without strong causal design.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint"],
        "allowed_details_keys": ["outcome_name", "value", "unit", "sample_size", "observation_type"],
    },
    "simulation": {
        "description": "Evidence produced from a simulation or computational model.",
        "allowed_context_keys": ["setting", "timepoint", "model_system"],
        "allowed_details_keys": ["outcome_name", "value", "unit", "model_name", "assumptions"],
    },
    "replication": {
        "description": "Evidence explicitly framed as a replication or reproduction attempt.",
        "allowed_context_keys": ["population", "species", "setting", "timepoint"],
        "allowed_details_keys": ["outcome_name", "effect_size", "unit", "ci_low", "ci_high", "p_value", "sample_size", "replication_target"],
    },
    "meta_analysis": {
        "description": "Evidence aggregated across multiple studies.",
        "allowed_context_keys": ["population", "species", "setting"],
        "allowed_details_keys": ["outcome_name", "effect_size", "unit", "ci_low", "ci_high", "p_value", "study_count", "sample_size"],
    },
    "literature_review": {
        "description": "Evidence summarized from prior literature without a new quantitative aggregation.",
        "allowed_context_keys": ["population", "species", "setting", "citation_context"],
        "allowed_details_keys": ["outcome_name", "review_scope", "study_count"],
    },
    "theoretical_argument": {
        "description": "Evidence derived from conceptual or theoretical reasoning.",
        "allowed_context_keys": ["section_type", "setting"],
        "allowed_details_keys": ["argument_type", "assumptions"],
    },
    "mathematical_derivation": {
        "description": "Evidence derived from formal or mathematical derivation.",
        "allowed_context_keys": ["section_type", "setting"],
        "allowed_details_keys": ["derivation_kind", "equation_refs", "assumptions"],
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
