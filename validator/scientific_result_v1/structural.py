"""Load the canonical v1.1 schema; do not maintain a second model definition."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def schema_path(claims_repo: Path | None = None) -> Path:
    root = (
        claims_repo if claims_repo is not None else Path(__file__).resolve().parents[2]
    )
    relative = Path("schemas/scientific-result-extraction/v1.1/schema.json")
    candidate = root / relative
    if claims_repo is not None or candidate.is_file():
        return candidate
    return Path(sys.prefix) / "share/claims" / relative


def load_schema(claims_repo: Path | None = None) -> dict[str, Any]:
    return json.loads(schema_path(claims_repo).read_text(encoding="utf-8"))


def structural_issues(
    payload: Any, *, claims_repo: Path | None = None
) -> list[dict[str, str]]:
    """JSON structure only; semantic x-validation_checks require additional checks."""
    from jsonschema import Draft202012Validator

    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        return [{"path": "$", "message": "Not finite JSON: " + str(exc)}]
    schema = load_schema(claims_repo)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    return [
        {"path": error.json_path, "message": error.message}
        for error in sorted(
            validator.iter_errors(payload), key=lambda error: error.json_path
        )
    ]
