from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifact_models import Artifact


AGENT_JSON_SCHEMA_FILENAME = "agent_schema.json"


def agent_json_schema() -> dict[str, Any]:
    schema = Artifact.model_json_schema()
    schema["$id"] = "https://claims-subnet.local/schemas/agent_v1_output.schema.json"
    schema["title"] = "Claims Agent V1 Structured Output"
    schema.setdefault(
        "description",
        "Claims-owned structured agent schema derived from the ARA markdown artifact model.",
    )
    return schema


def write_agent_json_schema(output_path: Path) -> None:
    output_path.write_text(
        json.dumps(agent_json_schema(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

