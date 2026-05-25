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


def load_example_paper() -> dict[str, Any]:
    return load_json(EXAMPLES_DIR / "input" / "paper.json")


def load_example_span() -> dict[str, Any]:
    return load_json(EXAMPLES_DIR / "input" / "span.json")


def build_task_envelope() -> dict[str, Any]:
    paper = load_example_paper()
    return {
        "task_id": "task-hanlon-001",
        "task_family": "claim_extraction",
        "schema_version": "0.1",
        "paper_id": paper["paper_id"],
        "expected_output": "extraction",
    }


def make_claim_handle(subject: str, predicate: str, obj: str) -> str:
    normalized = "|".join(part.strip().lower() for part in [subject, predicate, obj])
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"claim-{digest}"
