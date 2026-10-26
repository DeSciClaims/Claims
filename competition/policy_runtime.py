"""Credential-free fingerprint of competition control, scoring and worker code."""

from __future__ import annotations

import hashlib
from pathlib import Path


def policy_runtime_config() -> dict[str, dict[str, str]]:
    root = Path(__file__).resolve().parent
    return {"source_sha256": {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*.py"))
    }}
