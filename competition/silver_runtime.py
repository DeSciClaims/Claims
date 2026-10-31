"""Credential-free Silver runtime fingerprint for the weekly rules hash."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from validator.agent_v1.pairing import DEFAULT_EMBEDDING_MODEL


SILVER_SOURCE_FILES = (
    "adjudication_config.py",
    "adjudication_passes.py",
    "adjudication_runner.py",
    "eligibility.py",
    "eligibility_compact.py",
    "eligibility_dspy.py",
    "eligibility_decisions.py",
    "eligibility_decision_policy.py",
    "comparison_dspy.py",
    "canonicalization_dspy.py",
    "model_usage.py",
    "equivalence.py",
    "file_agent_workflow.py",
    "orchestrator.py",
    "pairing.py",
    "relation_classifier.py",
    "silver_builder.py",
    "silver_scoring.py",
)
SILVER_ENV_DEFAULTS = {
    "CLAIMS_SILVER_ADJUDICATION_BATCH_INPUT_TOKENS": "120000",
    "CLAIMS_SILVER_ADJUDICATION_BATCH_MAX_TOKENS": "16384",
    "CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES": "1",
    "CLAIMS_SILVER_ADJUDICATION_FALLBACK_MAX_CALLS": "256",
    "CLAIMS_SILVER_ADJUDICATION_WALL_TIMEOUT": "1800.0",
    "CLAIMS_SILVER_PAIRING_EMBEDDING_MODE": "",
    "CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL": DEFAULT_EMBEDDING_MODEL,
    "CLAIMS_SILVER_PAIRING_EMBEDDING_API_BASE": "https://openrouter.ai/api/v1",
    "CLAIMS_SILVER_PAIRING_EMBEDDING_THRESHOLD": "0.64",
    "CLAIMS_SILVER_PAIRING_HIGH_SIMILARITY": "0.88",
    "CLAIMS_SILVER_PAIRING_EMBEDDING_TIMEOUT": "60.0",
    "CLAIMS_SILVER_PAIRING_TOP_K": "4",
    "CLAIMS_SILVER_PAIRING_MAX_DENSE_PAIRS": "0",
    "CLAIMS_SILVER_CONSOLIDATION_TOP_K": "4",
    "CLAIMS_SILVER_PAIRING_SPAN_PAGE_PROXIMITY": "1",
    "CLAIMS_SILVER_ADJUDICATION_BATCH_SIZE": "4",
    "CLAIMS_SILVER_ADJUDICATION_MAX_WORKERS": "4",
    "CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_TOKENS_PER_SECOND": "100000",
    "CLAIMS_SILVER_ADJUDICATION_DECISIONS_TIMEOUT": "120",
    "CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_COST_PER_MILLION_USD": "",
    "CLAIMS_SILVER_MAX_ELIGIBLE_CLAIMS_PER_MINER": "10",
    "CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER": "80",
    "CLAIMS_SILVER_FILTER_BY_ASSESSMENT": "false",
    "CLAIMS_SILVER_EQUIVALENCE_SHARED_COVERAGE": "true",
}


def silver_pipeline_kwargs(settings: Mapping[str, str]) -> dict[str, int | bool]:
    """Convert frozen Silver limits into canonical pipeline arguments."""
    integer_keys = {
        "CLAIMS_SILVER_ADJUDICATION_BATCH_SIZE": "adjudication_batch_size",
        "CLAIMS_SILVER_ADJUDICATION_MAX_WORKERS": "adjudication_max_workers",
        "CLAIMS_SILVER_MAX_ELIGIBLE_CLAIMS_PER_MINER": "max_eligible_claims_per_miner",
        "CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER": "max_adjudication_cases",
    }
    boolean_keys = {
        "CLAIMS_SILVER_FILTER_BY_ASSESSMENT": "filter_by_assessment",
        "CLAIMS_SILVER_EQUIVALENCE_SHARED_COVERAGE": "equivalence_shared_coverage",
    }
    parsed: dict[str, int | bool] = {}
    for source, target in integer_keys.items():
        value = settings[source]
        if not value.isdecimal() or int(value) < 1:
            raise ValueError(f"invalid frozen Silver limit: {source}")
        parsed[target] = int(value)
    for source, target in boolean_keys.items():
        value = settings[source].lower()
        if value not in {"true", "false"}:
            raise ValueError(f"invalid frozen Silver flag: {source}")
        parsed[target] = value == "true"
    rate = settings["CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_TOKENS_PER_SECOND"]
    timeout = settings["CLAIMS_SILVER_ADJUDICATION_DECISIONS_TIMEOUT"]
    price = settings["CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_COST_PER_MILLION_USD"]
    import math
    try:
        if (not rate.isdecimal() or not 0 <= int(rate) <= 100_000_000
            or not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 600
            or price and (not math.isfinite(float(price)) or float(price) < 0)):
            raise ValueError("invalid frozen Decision API limits")
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid frozen Decision API limits") from exc
    return parsed


def silver_runtime_config() -> dict[str, Any]:
    """Capture prompt/scorer source and effective settings, never credential bytes.

    The module hashes cover the built-in adjudication prompts, embedding
    request and candidate-text logic, and score-building path. The model and
    endpoint are frozen independently of the secret used to authenticate.
    """
    source_dir = Path(__file__).resolve().parents[1] / "validator" / "agent_v1"
    source_hashes = {
        name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        for name in SILVER_SOURCE_FILES
    }
    source_hashes.update({
        str(path.relative_to(source_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((source_dir / "skills").glob("claims-silver-*/SKILL.md"))
    })
    settings = {name: os.getenv(name, default)
                for name, default in SILVER_ENV_DEFAULTS.items()}
    silver_pipeline_kwargs(settings)
    credential_env = os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_API_KEY_ENV", "OPENROUTER_API_KEY")
    if not credential_env:
        raise ValueError("embedding credential environment variable is empty")
    mode = settings["CLAIMS_SILVER_PAIRING_EMBEDDING_MODE"].strip().lower()
    embedding_enabled = mode in {"openrouter", "on", "true", "1"}
    return {
        "source_sha256": source_hashes,
        "settings": settings,
        "embedding_credential_env": credential_env,
        "embedding_credential_present": bool(os.getenv(credential_env)) if embedding_enabled else False,
    }
