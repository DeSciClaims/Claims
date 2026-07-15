from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..skillpack import SkillPack


class AgentRequest(BaseModel):
    pipeline_name: str = "agent_v1"
    output_schema: str = "agent_v1"
    paper: dict[str, Any]
    source_payload_path: str
    output_schema_path: str = "agent_schema.json"
    validation_feedback_path: str | None = None
    expected_output_path: str = "agent_output.json"


class AgentResult(BaseModel):
    output_path: Path
    manifest: dict[str, Any] = Field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""


class AgentRuntime(Protocol):
    runtime_name: str

    def run_skill(self, *, skill_pack: SkillPack, run_dir: Path, request: AgentRequest) -> AgentResult:
        ...
