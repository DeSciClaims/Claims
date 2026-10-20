"""Decision-only diagnostic adapter retaining deterministic checks and score inputs."""

import time
from pathlib import Path
from typing import Literal
from types import SimpleNamespace

from pydantic import Field

from miner.agent_v1.runtime.usage import merge_usage
from .decision_runtime import StrictDecision
from .decision_workflow import classify_slots


class Issue(StrictDecision):
    dimension: Literal[
        "falsifiability_quality",
        "scope_calibration",
        "argument_coherence",
        "exploration_integrity",
        "methodological_rigor",
        "grounding_adjudication",
    ]
    severity: Literal["critical", "major", "minor", "warning", "suggestion"]


class DiagnosticDecision(StrictDecision):
    evidence_status: Literal[
        "supported", "partially_supported", "unsupported", "unverifiable"
    ]
    paper_relevance: Literal["central", "supporting", "peripheral"]
    priority_rank: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    issues: list[Issue]


def run_decision_diagnostics(*, config, run_id, paper_id, submissions):
    from .diagnostic_batch import (
        DiagnosticBatchExecution,
        DiagnosticSubmissionReport,
        DiagnosticClaimAssessment,
        DiagnosticFinding,
        _claim_assessment_cases,
        _model_claim_assessment_payload,
        _materialize_claim_assessment,
        _diagnostic_finding_rows,
    )
    from .file_agent_workflow import FileAgentWorkflowConfig
    from .decision_runtime import DecisionRuntime
    import json

    c = FileAgentWorkflowConfig.from_env()
    root = config.root / run_id / paper_id / "decision"
    root.mkdir(parents=True, exist_ok=True)
    events = []

    class Session:
        pass

    session = Session()
    session.config = c
    session.root = root
    session.paper_id = paper_id
    session.workspace_id = f"{run_id}:diagnostics"
    session.request_gate = None
    session.usage_sink = events.append
    session._record_manifest_stage = lambda stage: None
    reports = {}
    started = time.perf_counter()
    for submission in submissions:
        cases = _claim_assessment_cases(
            submission.artifact, submission.source_payload or {}
        )
        rows = [{"slot": case["claim_ref"]} for case in cases]
        task = {
            "claim_cases": _model_claim_assessment_payload(cases),
            "artifact": submission.artifact,
            "structural_findings": submission.structural_findings,
            "grounding_findings": submission.grounding_findings,
            "instructions": (
                Path(__file__).parent / "skills/claims-diagnostic-batch/SKILL.md"
            ).read_text()
            + "\nDecision contract replaces serialization: classify every supplied claim independently. "
            "Return evidence status, relevance, priority, confidence and categorized rigor issues; "
            "no narrative. Do not suppress deterministic findings. Ignore file-writing instructions.",
        }
        values = classify_slots(
            session,
            stage=f"diagnostic_{submission.submission_ref}",
            rows=rows,
            value_model=DiagnosticDecision,
            model=config.model,
            task=task,
        )
        assessments = []
        findings = []
        for case in cases:
            value = values[case["claim_ref"]]
            assessment = DiagnosticClaimAssessment(
                claim_ref=case["claim_ref"],
                evidence_status=value["evidence_status"],
                paper_relevance=value["paper_relevance"],
                priority_rank=value["priority_rank"],
                reason="decision_" + value["evidence_status"],
            )
            assessments.append(_materialize_claim_assessment(assessment, case))
            for issue in value["issues"]:
                findings.append(
                    DiagnosticFinding(
                        **issue,
                        target_type="claim",
                        target_id=case["claim_id"],
                        message="decision_" + issue["dimension"],
                        metadata={"confidence": value["confidence"]},
                    )
                )
        report = DiagnosticSubmissionReport(
            submission_ref=submission.submission_ref,
            claim_assessments=[],
            findings=findings,
        )
        reports[submission.submission_ref] = {
            "claim_assessments": assessments,
            "findings": _diagnostic_finding_rows(report, assessments),
        }
    (root / "usage_events.json").write_text(json.dumps(events, indent=2, default=str))
    return DiagnosticBatchExecution(
        reports=reports,
        usage=merge_usage([e["usage"] for e in events]),
        duration_seconds=time.perf_counter() - started,
        operation_id=f"{run_id}:{paper_id}:diagnostic-paper",
        workspace=root,
        usage_events=tuple(events),
    )
