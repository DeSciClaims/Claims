from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RIGOR_FINDINGS_SCHEMA_FILENAME = "rigor_findings_schema.json"


def rigor_findings_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Claims Agent V1 Rigor Findings",
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["dimension", "severity", "message"],
                    "properties": {
                        "dimension": {
                            "type": "string",
                            "enum": [
                                "evidence_relevance",
                                "falsifiability_quality",
                                "scope_calibration",
                                "argument_coherence",
                                "exploration_integrity",
                                "methodological_rigor",
                            ],
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "major", "minor", "warning", "suggestion"],
                        },
                        "target_type": {"type": ["string", "null"]},
                        "target_id": {"type": ["string", "null"]},
                        "message": {"type": "string", "minLength": 1},
                        "evidence_span": {"type": ["string", "null"]},
                        "suggestion": {"type": ["string", "null"]},
                        "metadata": {"type": "object"},
                    },
                },
            }
        },
    }


def write_rigor_findings_schema(path: Path) -> None:
    path.write_text(json.dumps(rigor_findings_schema(), indent=2, ensure_ascii=False), encoding="utf-8")
