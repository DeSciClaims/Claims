from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
SCHEMAS_DIR = REPO_ROOT / "schemas"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_example_source() -> dict[str, Any]:
    return load_json(EXAMPLES_DIR / "input" / "source.json")


def load_example_chunk() -> dict[str, Any]:
    return load_json(EXAMPLES_DIR / "input" / "chunk.json")


def build_task_envelope() -> dict[str, Any]:
    source = load_example_source()
    return {
        "task_id": "task-hanlon-001",
        "task_family": "claim_extraction",
        "schema_version": "0.1",
        "source_id": source["source_id"],
        "expected_output": "extraction",
    }


def make_claim_handle(subject: str, predicate: str, obj: str) -> str:
    normalized = "|".join(part.strip().lower() for part in [subject, predicate, obj])
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"claim-{digest}"
