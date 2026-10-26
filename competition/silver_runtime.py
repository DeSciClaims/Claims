"""Credential-free Silver runtime fingerprint for the weekly rules hash."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from validator.agent_v1.pairing import DEFAULT_EMBEDDING_MODEL


SILVER_SOURCE_FILES = (
    "adjudication_config.py",
    "adjudication_passes.py",
    "adjudication_runner.py",
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
}


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
    settings = {name: os.getenv(name, default)
                for name, default in SILVER_ENV_DEFAULTS.items()}
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
