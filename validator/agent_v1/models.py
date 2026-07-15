from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Severity = Literal["blocker", "critical", "major", "minor", "warning", "suggestion"]
PassName = Literal["structural", "grounding", "rigor", "scoring"]


class AgentV1ValidationFinding(BaseModel):
    finding_id: str = ""
    pass_name: PassName
    dimension: str
    severity: Severity
    target_type: str | None = None
    target_id: str | None = None
    message: str
    evidence_span: str | None = None
    suggestion: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentV1PassSummary(BaseModel):
    passed: bool
    finding_count: int = 0
    runtime: str | None = None


class AgentV1ValidationMetrics(BaseModel):
    elapsed_seconds: float = 0.0
    rigor_agent_elapsed_seconds: float | None = None
    token_usage: dict[str, int | None] = Field(
        default_factory=lambda: {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    )
    cost_usd: float | None = None
    usage_source: str = "unavailable"


class AgentV1ValidationReport(BaseModel):
    validator_version: str = "agent_v1"
    artifact_path: str
    source_payload_path: str | None = None
    paper_id: str | None = None
    passed: bool
    score: float
    threshold: float = 0.7
    summary: dict[str, int] = Field(default_factory=dict)
    passes: dict[str, AgentV1PassSummary] = Field(default_factory=dict)
    findings: list[AgentV1ValidationFinding] = Field(default_factory=list)
    metrics: AgentV1ValidationMetrics = Field(default_factory=AgentV1ValidationMetrics)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RigorAgentRequest(BaseModel):
    artifact_path: str = "agent_output.json"
    source_payload_path: str | None = "source_payload.json"
    structural_findings_path: str = "structural_findings.json"
    grounding_findings_path: str = "grounding_findings.json"
    output_schema_path: str = "rigor_findings_schema.json"
    expected_output_path: str = "rigor_findings.json"


class RigorAgentResult(BaseModel):
    output_path: str
    stdout: str = ""
    stderr: str = ""
    manifest: dict[str, Any] = Field(default_factory=dict)
