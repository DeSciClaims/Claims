from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from miner.agent_v1.runtime.usage import empty_usage, merge_usage, usage_from_cli_process

from .file_agent_workflow import (
    FileAgentWorkflowConfig,
    _agent_command,
    _run_until_valid_output,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticFinding(_StrictModel):
    dimension: Literal[
        "evidence_relevance",
        "falsifiability_quality",
        "scope_calibration",
        "argument_coherence",
        "exploration_integrity",
        "methodological_rigor",
        "grounding_adjudication",
    ]
    severity: Literal["critical", "major", "minor", "warning", "suggestion"]
    target_type: str | None = None
    target_id: str | None = None
    message: str = Field(min_length=1)
    evidence_span: str | None = None
    suggestion: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiagnosticClaimAssessment(_StrictModel):
    claim_ref: str = Field(min_length=1)
    evidence_status: Literal[
        "supported",
        "partially_supported",
        "unsupported",
        "unverifiable",
    ]
    paper_relevance: Literal["central", "supporting", "peripheral"]
    priority_rank: int = Field(ge=1)
    reason: str = Field(min_length=1)
    unsupported_assertions: list[str] = Field(default_factory=list)


class DiagnosticSubmissionReport(_StrictModel):
    submission_ref: str = Field(min_length=1)
    claim_assessments: list[DiagnosticClaimAssessment]
    findings: list[DiagnosticFinding] = Field(default_factory=list)


class DiagnosticBatchOutput(_StrictModel):
    reports: list[DiagnosticSubmissionReport]


class DiagnosticAgentClaimAssessment(_StrictModel):
    evidence_status: Literal[
        "supported",
        "partially_supported",
        "unsupported",
        "unverifiable",
    ]
    paper_relevance: Literal["central", "supporting", "peripheral"]
    priority_rank: int = Field(ge=1)
    reason: str = Field(min_length=1)
    unsupported_assertions: list[str] = Field(default_factory=list)


class DiagnosticAgentSubmissionReport(_StrictModel):
    claim_assessments: dict[str, DiagnosticAgentClaimAssessment]
    findings: list[DiagnosticFinding] = Field(default_factory=list)


class DiagnosticAgentOutput(_StrictModel):
    reports: dict[str, DiagnosticAgentSubmissionReport]


@dataclass(frozen=True)
class DiagnosticBatchConfig:
    root: Path
    harness: str
    provider: str
    model: str
    command_template: str = ""
    batch_size: int = 1
    max_turns: int = 30
    max_tokens: int = 16384
    timeout_seconds: float = 1800.0
    output_poll_seconds: float = 0.5
    output_stable_seconds: float = 1.0
    usage_grace_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> DiagnosticBatchConfig:
        return cls(
            root=Path(
                os.getenv(
                    "CLAIMS_DIAGNOSTIC_FILE_WORKSPACE_ROOT",
                    "/tmp/claims-diagnostic-workspaces",
                )
            ).expanduser(),
            harness=os.getenv("CLAIMS_RIGOR_HARNESS", "").strip().lower().replace("_", "-"),
            provider=os.getenv("CLAIMS_RIGOR_PROVIDER", "openrouter").strip() or "openrouter",
            model=os.getenv(
                "CLAIMS_RIGOR_MODEL",
                os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MODEL", "openai/gpt-4o-mini"),
            ).strip(),
            command_template=os.getenv(
                "CLAIMS_DIAGNOSTIC_FILE_AGENT_CLI_COMMAND_TEMPLATE", ""
            ).strip(),
            batch_size=max(1, int(os.getenv("CLAIMS_DIAGNOSTIC_MINER_BATCH_SIZE", "1") or 1)),
            max_turns=max(1, int(os.getenv("CLAIMS_RIGOR_MAX_TURNS", "30") or 30)),
            max_tokens=max(
                1024,
                int(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_TOKENS", "16384") or 16384),
            ),
            timeout_seconds=max(
                1.0,
                float(
                    os.getenv(
                        "CLAIMS_DIAGNOSTIC_FILE_AGENT_TIMEOUT",
                        os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_TIMEOUT", "1800"),
                    )
                    or 1800
                ),
            ),
            output_poll_seconds=max(
                0.1,
                float(os.getenv("CLAIMS_DIAGNOSTIC_OUTPUT_POLL_SECONDS", "0.5") or 0.5),
            ),
            output_stable_seconds=max(
                0.0,
                float(os.getenv("CLAIMS_DIAGNOSTIC_OUTPUT_STABLE_SECONDS", "1.0") or 1.0),
            ),
            usage_grace_seconds=max(
                0.0,
                float(os.getenv("CLAIMS_DIAGNOSTIC_USAGE_GRACE_SECONDS", "15.0") or 15.0),
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.batch_size > 1 and self.harness in {
            "hermes-cli",
            "codex-cli",
            "claude-cli",
        }


@dataclass(frozen=True)
class DiagnosticBatchSubmission:
    submission_ref: str
    artifact: dict[str, Any]
    source_payload: dict[str, Any] | None
    structural_findings: list[dict[str, Any]]
    grounding_findings: list[dict[str, Any]]


@dataclass(frozen=True)
class DiagnosticBatchExecution:
    reports: dict[str, dict[str, Any]]
    usage: dict[str, Any]
    duration_seconds: float
    operation_id: str
    workspace: Path
    error: str | None = None
    usage_events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _DiagnosticValidation:
    reports: dict[str, dict[str, Any]]
    accepted_payload: dict[str, Any]
    repair_claim_refs: dict[str, list[str]]
    errors: tuple[str, ...]


def run_diagnostic_batch(
    *,
    config: DiagnosticBatchConfig,
    run_id: str,
    paper_id: str,
    submissions: list[DiagnosticBatchSubmission],
) -> DiagnosticBatchExecution:
    operation_id = f"{run_id}:{paper_id}:diagnostic-paper"
    root = config.root / _safe_path(run_id) / _safe_path(paper_id) / "paper"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    source_paths: dict[str, Path] = {}
    task_rows: list[dict[str, Any]] = []
    claim_cases_by_submission: dict[str, dict[str, dict[str, Any]]] = {}
    for submission in submissions:
        source_payload = submission.source_payload or {}
        source_hash = hashlib.sha256(_json_bytes(source_payload)).hexdigest()
        source_path = source_paths.get(source_hash)
        if source_path is None:
            source_path = root / "sources" / f"source_{source_hash[:16]}.json"
            _write_json(source_path, source_payload)
            source_paths[source_hash] = source_path
        submission_dir = root / "submissions" / _safe_path(submission.submission_ref)
        artifact_path = submission_dir / "agent_output.json"
        structural_path = submission_dir / "structural_findings.json"
        grounding_path = submission_dir / "grounding_findings.json"
        claim_cases_path = submission_dir / "claim_assessment_cases.json"
        claim_cases = _claim_assessment_cases(submission.artifact, source_payload)
        claim_cases_by_submission[submission.submission_ref] = {
            row["claim_ref"]: row for row in claim_cases
        }
        _write_json(artifact_path, submission.artifact)
        _write_json(structural_path, submission.structural_findings)
        _write_json(grounding_path, submission.grounding_findings)
        _write_json(claim_cases_path, _model_claim_assessment_payload(claim_cases))
        task_rows.append(
            {
                "submission_ref": submission.submission_ref,
                "artifact_path": str(artifact_path),
                "source_payload_path": str(source_path),
                "structural_findings_path": str(structural_path),
                "grounding_findings_path": str(grounding_path),
                "claim_assessment_cases_path": str(claim_cases_path),
                "required_claim_refs": [row["claim_ref"] for row in claim_cases],
            }
        )

    expected_submission_refs = {submission.submission_ref for submission in submissions}
    skill_source = (
        Path(__file__).resolve().parent
        / "skills"
        / "claims-diagnostic-batch"
        / "SKILL.md"
    )
    if not skill_source.is_file():
        raise RuntimeError(f"Diagnostic batch skill is missing: {skill_source}")
    skill_path = root / "SKILL.md"
    skill_path.write_text(skill_source.read_text(encoding="utf-8"), encoding="utf-8")
    task_path = root / "task.json"
    schema_path = root / "output_schema.json"
    skeleton_path = root / "output_skeleton.json"
    output_path = root / "output.json"
    stdout_path = root / "stdout.log"
    stderr_path = root / "stderr.log"
    output_path.unlink(missing_ok=True)
    _write_json(skeleton_path, _output_skeleton(claim_cases_by_submission))
    _write_json(
        task_path,
        {
            "paper_ref": "P0001",
            "submissions": task_rows,
            "output_skeleton_path": str(skeleton_path),
            "requirements": {
                "review_every_submission_independently": True,
                "fill_every_preallocated_submission_and_claim_slot": True,
                "do_not_add_remove_or_rename_identifier_keys": True,
                "do_not_compare_rank_or_copy_findings_between_submissions": True,
                "use_only_each_submission_artifact_and_its_linked_source_payload": True,
            },
        },
    )
    _write_json(schema_path, DiagnosticAgentOutput.model_json_schema())

    file_config = FileAgentWorkflowConfig(
        root=config.root,
        harness=config.harness,
        provider=config.provider,
        comparison_model=config.model,
        canonicalization_model=config.model,
        command_template=config.command_template,
        max_turns=config.max_turns,
        max_tokens=config.max_tokens,
        timeout_seconds=config.timeout_seconds,
        output_poll_seconds=config.output_poll_seconds,
        output_stable_seconds=config.output_stable_seconds,
        usage_grace_seconds=config.usage_grace_seconds,
    )
    command = _agent_command(file_config, model=config.model, stage_key="diagnostic_batch")
    query = "\n".join(
        [
            "Run this Claims paper-level diagnostic validation task.",
            f"Read the complete skill instructions from {skill_path}.",
            f"Read the complete task from {task_path}.",
            f"Use the validator-owned identifier skeleton at {skeleton_path}.",
            f"Validate the result against {schema_path}.",
            f"Write exactly one JSON object to {output_path}.",
            "After writing valid output, finish immediately without another tool or model call.",
            "Do not modify the task, schema, skeleton, source, or submission files.",
        ]
    )
    overall_started = time.perf_counter()
    usage_events: list[dict[str, Any]] = []
    initial_error: str | None = None
    initial_payload: dict[str, Any] = {}
    try:
        _, usage, operation_duration = _invoke_diagnostic_agent(
            config=config,
            command=command,
            root=root,
            output_path=output_path,
            output_model=DiagnosticAgentOutput,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            query=query,
        )
        usage_events.append(
            _usage_event(
                operation_id=operation_id,
                usage=usage,
                duration_seconds=operation_duration,
                status="success" if output_path.is_file() else "failed",
            )
        )
        initial_payload = _read_json_object(output_path)
    except Exception as exc:
        initial_error = f"{type(exc).__name__}: {exc}"
        recorded_event = next(
            (
                event
                for event in usage_events
                if event["operation_id"] == operation_id
            ),
            None,
        )
        if recorded_event is not None:
            recorded_event["status"] = "failed"
            recorded_event["error"] = initial_error
        else:
            usage_events.append(
                _usage_event(
                    operation_id=operation_id,
                    usage=empty_usage("diagnostic_batch_failed"),
                    duration_seconds=time.perf_counter() - overall_started,
                    status="failed",
                    error=initial_error,
                )
            )

    initial_validation = _validate_agent_payload(
        initial_payload,
        expected_submission_refs=expected_submission_refs,
        claim_cases_by_submission=claim_cases_by_submission,
    )
    validation = initial_validation
    repair_attempted = bool(validation.repair_claim_refs)
    repair_error: str | None = None
    if repair_attempted:
        repair_operation_id = f"{operation_id}-repair"
        repair_task_path = root / "repair_task.json"
        repair_schema_path = root / "repair_output_schema.json"
        repair_skeleton_path = root / "repair_output_skeleton.json"
        repair_output_path = root / "repair_output.json"
        repair_stdout_path = root / "repair_stdout.log"
        repair_stderr_path = root / "repair_stderr.log"
        repair_output_path.unlink(missing_ok=True)
        repair_rows = [
            {
                **row,
                "required_claim_refs": validation.repair_claim_refs[row["submission_ref"]],
            }
            for row in task_rows
            if row["submission_ref"] in validation.repair_claim_refs
        ]
        repair_cases = {
            submission_ref: {
                claim_ref: claim_cases_by_submission[submission_ref][claim_ref]
                for claim_ref in claim_refs
            }
            for submission_ref, claim_refs in validation.repair_claim_refs.items()
        }
        _write_json(repair_skeleton_path, _output_skeleton(repair_cases))
        _write_json(repair_schema_path, DiagnosticAgentOutput.model_json_schema())
        _write_json(
            repair_task_path,
            {
                "paper_ref": "P0001",
                "submissions": repair_rows,
                "output_skeleton_path": str(repair_skeleton_path),
                "validator_rejection": list(validation.errors),
                "requirements": {
                    "repair_only_the_preallocated_missing_or_invalid_claim_slots": True,
                    "fill_every_preallocated_submission_and_claim_slot": True,
                    "do_not_add_remove_or_rename_identifier_keys": True,
                    "do_not_regenerate_already_valid_claim_assessments": True,
                },
            },
        )
        repair_command = _agent_command(
            file_config,
            model=config.model,
            stage_key="diagnostic_batch_repair",
        )
        repair_query = "\n".join(
            [
                "Repair only the missing or invalid Claims diagnostic assessment slots.",
                f"Read the complete skill instructions from {skill_path}.",
                f"Read the targeted repair task from {repair_task_path}.",
                f"Use the validator-owned repair skeleton at {repair_skeleton_path}.",
                f"Validate the result against {repair_schema_path}.",
                f"Write exactly one JSON object to {repair_output_path}.",
                "Do not regenerate submissions or claims absent from the repair skeleton.",
                "After writing valid output, finish immediately without another tool or model call.",
            ]
        )
        repair_started = time.perf_counter()
        try:
            _, repair_usage, repair_duration = _invoke_diagnostic_agent(
                config=config,
                command=repair_command,
                root=root,
                output_path=repair_output_path,
                output_model=DiagnosticAgentOutput,
                stdout_path=repair_stdout_path,
                stderr_path=repair_stderr_path,
                query=repair_query,
            )
            usage_events.append(
                _usage_event(
                    operation_id=repair_operation_id,
                    usage=repair_usage,
                    duration_seconds=repair_duration,
                    status="success" if repair_output_path.is_file() else "failed",
                )
            )
            validation = _validate_agent_payload(
                _merge_repair_payload(
                    validation.accepted_payload,
                    _read_json_object(repair_output_path),
                    repair_claim_refs=validation.repair_claim_refs,
                ),
                expected_submission_refs=expected_submission_refs,
                claim_cases_by_submission=claim_cases_by_submission,
            )
        except Exception as exc:
            repair_error = f"{type(exc).__name__}: {exc}"
            recorded_event = next(
                (
                    event
                    for event in usage_events
                    if event["operation_id"] == repair_operation_id
                ),
                None,
            )
            if recorded_event is not None:
                recorded_event["status"] = "failed"
                recorded_event["error"] = repair_error
            else:
                usage_events.append(
                    _usage_event(
                        operation_id=repair_operation_id,
                        usage=empty_usage("diagnostic_batch_repair_failed"),
                        duration_seconds=time.perf_counter() - repair_started,
                        status="failed",
                        error=repair_error,
                    )
                )

    reports = validation.reports
    unresolved_error = None
    if validation.repair_claim_refs:
        unresolved_error = (
            "Diagnostic output remained incomplete after one targeted repair; "
            + "; ".join(validation.errors)
        )
    error = repair_error or unresolved_error
    duration = time.perf_counter() - overall_started
    usage = merge_usage([event["usage"] for event in usage_events])
    _write_json(
        root / "manifest.json",
        {
            "schema": "claims_diagnostic_paper_v3",
            "operation_id": operation_id,
            "paper_id": paper_id,
            "submission_count": len(submissions),
            "claim_count": sum(len(rows) for rows in claim_cases_by_submission.values()),
            "completed_report_count": len(reports),
            "source_payload_count": len(source_paths),
            "harness": config.harness,
            "model": config.model,
            "duration_seconds": round(duration, 3),
            "status": "complete" if not error else "partial" if reports else "failed",
            "repair_attempted": repair_attempted,
            "initial_error": initial_error,
            "initial_validation_errors": list(initial_validation.errors),
            "error": error,
        },
    )
    return DiagnosticBatchExecution(
        reports=reports,
        usage=usage,
        duration_seconds=duration,
        operation_id=operation_id,
        workspace=root,
        error=error,
        usage_events=tuple(usage_events),
    )


def _invoke_diagnostic_agent(
    *,
    config: DiagnosticBatchConfig,
    command: list[str],
    root: Path,
    output_path: Path,
    output_model: type[BaseModel],
    stdout_path: Path,
    stderr_path: Path,
    query: str,
) -> tuple[Any, dict[str, Any], float]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    completed = _run_until_valid_output(
        [*command, query],
        cwd=root,
        output_path=output_path,
        output_model=output_model,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=config.timeout_seconds,
        poll_seconds=config.output_poll_seconds,
        stable_seconds=config.output_stable_seconds,
        usage_grace_seconds=config.usage_grace_seconds,
        env=(
            {**os.environ, "HERMES_MAX_TOKENS": str(config.max_tokens)}
            if config.harness == "hermes-cli"
            else None
        ),
    )
    usage = usage_from_cli_process(
        command,
        completed.stdout,
        completed.stderr,
        cwd=root,
        started_at=started_at,
        model=config.model,
    )
    return completed, usage, time.perf_counter() - started


def precomputed_rigor_manifest(execution: DiagnosticBatchExecution) -> dict[str, Any]:
    return {
        "runtime": "diagnostic-file-paper",
        "elapsed_seconds": execution.duration_seconds,
        "usage": empty_usage("shared_diagnostic_paper"),
        "metadata": {
            "diagnostic_batch": True,
            "operation_id": execution.operation_id,
            "workspace": str(execution.workspace),
            "usage_recorded_separately": True,
        },
    }


def failed_diagnostic_report(message: str) -> dict[str, Any]:
    return {
        "claim_assessments": [],
        "findings": [
            {
                "dimension": "methodological_rigor",
                "severity": "critical",
                "target_type": "artifact",
                "target_id": None,
                "message": message,
                "evidence_span": None,
                "suggestion": "Retry diagnostic validation before allowing miner claims into Silver.",
                "metadata": {"code": "diagnostic_paper_review_failed"},
            }
        ],
    }


def _output_skeleton(
    claim_cases_by_submission: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "reports": {
            submission_ref: {
                "claim_assessments": {
                    claim_ref: None for claim_ref in sorted(claim_cases)
                },
                "findings": [],
            }
            for submission_ref, claim_cases in sorted(claim_cases_by_submission.items())
        }
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Diagnostic output must be a JSON object: {path}")
    return payload


def _canonical_agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reports = payload.get("reports")
    if isinstance(reports, dict):
        return {"reports": reports}
    if not isinstance(reports, list):
        return {"reports": {}}

    normalized: dict[str, Any] = {}
    for row in reports:
        if not isinstance(row, dict):
            continue
        submission_ref = str(row.get("submission_ref") or "")
        if not submission_ref or submission_ref in normalized:
            continue
        assessments = row.get("claim_assessments")
        assessment_map: dict[str, Any] = {}
        if isinstance(assessments, list):
            for assessment in assessments:
                if not isinstance(assessment, dict):
                    continue
                claim_ref = str(assessment.get("claim_ref") or "")
                if claim_ref and claim_ref not in assessment_map:
                    assessment_map[claim_ref] = {
                        key: value
                        for key, value in assessment.items()
                        if key != "claim_ref"
                    }
        elif isinstance(assessments, dict):
            assessment_map = assessments
        normalized[submission_ref] = {
            "claim_assessments": assessment_map,
            "findings": row.get("findings") if isinstance(row.get("findings"), list) else [],
        }
    return {"reports": normalized}


def _validate_agent_payload(
    payload: dict[str, Any],
    *,
    expected_submission_refs: set[str],
    claim_cases_by_submission: dict[str, dict[str, dict[str, Any]]],
) -> _DiagnosticValidation:
    canonical = _canonical_agent_payload(payload)
    raw_reports = canonical["reports"]
    reports: dict[str, dict[str, Any]] = {}
    accepted_reports: dict[str, Any] = {}
    repair_claim_refs: dict[str, list[str]] = {}
    errors: list[str] = []

    unexpected_submissions = sorted(set(raw_reports).difference(expected_submission_refs))
    if unexpected_submissions:
        errors.append(f"unknown submissions={unexpected_submissions}")
    for submission_ref in sorted(expected_submission_refs):
        cases = claim_cases_by_submission[submission_ref]
        raw_report = raw_reports.get(submission_ref)
        report_present = isinstance(raw_report, dict)
        if not report_present:
            raw_report = {}
        raw_assessments = raw_report.get("claim_assessments")
        if not isinstance(raw_assessments, dict):
            raw_assessments = {}
        accepted_assessments: dict[str, Any] = {}
        invalid_refs: list[str] = []
        unexpected_claims = sorted(set(raw_assessments).difference(cases))
        if unexpected_claims:
            errors.append(
                f"submission={submission_ref} unknown claims={unexpected_claims}"
            )
        for claim_ref in sorted(cases):
            raw_assessment = raw_assessments.get(claim_ref)
            if not isinstance(raw_assessment, dict):
                invalid_refs.append(claim_ref)
                continue
            normalized_assessment = _normalize_assessment_aliases(raw_assessment)
            try:
                assessment = DiagnosticAgentClaimAssessment.model_validate(
                    normalized_assessment
                )
            except Exception as exc:
                invalid_refs.append(claim_ref)
                errors.append(
                    f"submission={submission_ref} claim={claim_ref} invalid: {exc}"
                )
                continue
            accepted_assessments[claim_ref] = assessment.model_dump(mode="json")

        findings: list[DiagnosticFinding] = []
        raw_findings = raw_report.get("findings")
        if isinstance(raw_findings, list):
            for index, raw_finding in enumerate(raw_findings):
                try:
                    findings.append(DiagnosticFinding.model_validate(raw_finding))
                except Exception as exc:
                    errors.append(
                        f"submission={submission_ref} finding={index} ignored: {exc}"
                    )
        accepted_reports[submission_ref] = {
            "claim_assessments": accepted_assessments,
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }
        if not report_present or invalid_refs:
            repair_claim_refs[submission_ref] = invalid_refs or sorted(cases)
            if not report_present:
                errors.append(f"submission={submission_ref} report missing")
            elif invalid_refs:
                errors.append(
                    f"submission={submission_ref} missing or invalid claims={invalid_refs}"
                )
            continue

        assessments = [
            _materialize_claim_assessment(
                DiagnosticClaimAssessment(
                    claim_ref=claim_ref,
                    **accepted_assessments[claim_ref],
                ),
                cases[claim_ref],
            )
            for claim_ref in sorted(cases)
        ]
        report = DiagnosticSubmissionReport(
            submission_ref=submission_ref,
            claim_assessments=[
                DiagnosticClaimAssessment(
                    claim_ref=claim_ref,
                    **accepted_assessments[claim_ref],
                )
                for claim_ref in sorted(cases)
            ],
            findings=findings,
        )
        reports[submission_ref] = {
            "claim_assessments": assessments,
            "findings": _diagnostic_finding_rows(report, assessments),
        }

    return _DiagnosticValidation(
        reports=reports,
        accepted_payload={"reports": accepted_reports},
        repair_claim_refs=repair_claim_refs,
        errors=tuple(errors),
    )


def _normalize_assessment_aliases(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    status = str(normalized.get("evidence_status") or "").strip().lower()
    status = status.replace("-", "_").replace(" ", "_")
    if status == "partial_supported":
        status = "partially_supported"
    normalized["evidence_status"] = status
    return normalized


def _merge_repair_payload(
    accepted_payload: dict[str, Any],
    repair_payload: dict[str, Any],
    *,
    repair_claim_refs: dict[str, list[str]],
) -> dict[str, Any]:
    merged = json.loads(json.dumps(accepted_payload))
    merged_reports = merged.setdefault("reports", {})
    repair_reports = _canonical_agent_payload(repair_payload)["reports"]
    for submission_ref, allowed_claim_refs in repair_claim_refs.items():
        repair_report = repair_reports.get(submission_ref)
        if not isinstance(repair_report, dict):
            continue
        target_report = merged_reports.setdefault(
            submission_ref,
            {"claim_assessments": {}, "findings": []},
        )
        repair_assessments = repair_report.get("claim_assessments")
        if isinstance(repair_assessments, dict):
            for claim_ref in allowed_claim_refs:
                if claim_ref in repair_assessments:
                    target_report["claim_assessments"][claim_ref] = repair_assessments[
                        claim_ref
                    ]
        repair_findings = repair_report.get("findings")
        if isinstance(repair_findings, list) and repair_findings:
            target_report["findings"] = repair_findings
    return merged


def _usage_event(
    *,
    operation_id: str,
    usage: dict[str, Any],
    duration_seconds: float,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "usage": usage,
        "duration_seconds": duration_seconds,
        "status": status,
        "error": error,
    }


def _claim_assessment_cases(
    artifact: dict[str, Any],
    source_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    logic = artifact.get("logic") if isinstance(artifact.get("logic"), dict) else {}
    claims = logic.get("claims") if isinstance(logic.get("claims"), list) else []
    evidence_layer = artifact.get("evidence") if isinstance(artifact.get("evidence"), dict) else {}
    evidence_records = evidence_layer.get("records") if isinstance(evidence_layer.get("records"), list) else []
    evidence_by_id = {
        str(row.get("evidence_id")): row
        for row in evidence_records
        if isinstance(row, dict) and str(row.get("evidence_id") or "")
    }
    spans = source_payload.get("spans") if isinstance(source_payload.get("spans"), list) else []
    spans_by_id: dict[str, dict[str, Any]] = {}
    for row in spans:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id") or "")
        if span_id:
            spans_by_id[span_id] = row
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        reader_span_id = str(metadata.get("reader_span_id") or "")
        if reader_span_id:
            spans_by_id[reader_span_id] = row
    cases: list[dict[str, Any]] = []
    for index, claim in enumerate(row for row in claims if isinstance(row, dict)):
        claim_id = str(claim.get("claim_id") or f"claim_{index + 1}")
        evidence_ids = [
            str(value) for value in claim.get("evidence_ids", []) if str(value).strip()
        ] if isinstance(claim.get("evidence_ids"), list) else []
        source_refs = claim.get("sources") if isinstance(claim.get("sources"), list) else []
        source_span_ids = sorted({
            str(span_id)
            for ref in source_refs
            if isinstance(ref, dict)
            for span_id in (ref.get("span_ids") if isinstance(ref.get("span_ids"), list) else [])
            if str(span_id).strip()
        })
        linked_evidence = [
            _compact_evidence_record(evidence_by_id[evidence_id])
            for evidence_id in evidence_ids
            if evidence_id in evidence_by_id
        ]
        evidence_span_ids = {
            str(span_id)
            for record in linked_evidence
            for source_ref in record.get("source_refs", [])
            if isinstance(source_ref, dict)
            for span_id in source_ref.get("span_ids", [])
            if str(span_id).strip()
        }
        all_span_ids = sorted(set(source_span_ids) | evidence_span_ids)
        cases.append(
            {
                "claim_ref": f"c{index}",
                "claim_id": claim_id,
                "statement": str(claim.get("statement") or ""),
                "conditions": claim.get("conditions"),
                "falsification_criteria": claim.get("falsification_criteria"),
                "evidence_ids": evidence_ids,
                "source_span_ids": all_span_ids,
                "claim_source_refs": source_refs,
                "linked_evidence": linked_evidence,
                "source_spans": [
                    {
                        "span_id": str(spans_by_id[span_id].get("span_id") or span_id),
                        "section_name": spans_by_id[span_id].get("section_name"),
                        "page": spans_by_id[span_id].get("page"),
                        "text": spans_by_id[span_id].get("text"),
                    }
                    for span_id in all_span_ids
                    if span_id in spans_by_id
                ],
            }
        )
    return cases


def _compact_evidence_record(record: dict[str, Any]) -> dict[str, Any]:
    source_refs = record.get("source_refs") if isinstance(record.get("source_refs"), list) else []
    return {
        "evidence_id": str(record.get("evidence_id") or ""),
        "title": record.get("title"),
        "role": record.get("role"),
        "summary": record.get("summary"),
        "evidence_method": record.get("evidence_method"),
        "outcome_type": record.get("outcome_type"),
        "source_refs": source_refs,
    }


def _model_claim_assessment_payload(cases: list[dict[str, Any]]) -> dict[str, Any]:
    source_spans_by_id = {
        str(span.get("span_id")): span
        for case in cases
        for span in case.get("source_spans", [])
        if isinstance(span, dict) and str(span.get("span_id") or "")
    }
    span_aliases = {
        span_id: f"p{index}"
        for index, span_id in enumerate(sorted(source_spans_by_id))
    }
    return {
        "claims": [
            _model_claim_assessment_case(case, span_aliases)
            for case in cases
        ],
        "source_spans": [
            {
                "source_ref": span_aliases[span_id],
                "section_name": source_spans_by_id[span_id].get("section_name"),
                "page": source_spans_by_id[span_id].get("page"),
                "text": source_spans_by_id[span_id].get("text"),
            }
            for span_id in sorted(source_spans_by_id)
        ],
    }


def _model_claim_assessment_case(
    case: dict[str, Any],
    span_aliases: dict[str, str],
) -> dict[str, Any]:
    return {
        "claim_ref": case["claim_ref"],
        "statement": case["statement"],
        "conditions": case["conditions"],
        "falsification_criteria": case["falsification_criteria"],
        "claim_sources": [
            {
                "quote": source_ref.get("quote"),
                "role": source_ref.get("role"),
            }
            for source_ref in case.get("claim_source_refs", [])
            if isinstance(source_ref, dict)
        ],
        "linked_evidence": [
            {
                "title": record.get("title"),
                "role": record.get("role"),
                "summary": record.get("summary"),
                "evidence_method": record.get("evidence_method"),
                "outcome_type": record.get("outcome_type"),
                "source_quotes": [
                    {
                        "quote": source_ref.get("quote"),
                        "role": source_ref.get("role"),
                    }
                    for source_ref in record.get("source_refs", [])
                    if isinstance(source_ref, dict)
                ],
            }
            for record in case.get("linked_evidence", [])
            if isinstance(record, dict)
        ],
        "source_refs": sorted({
            span_aliases[str(span.get("span_id"))]
            for span in case.get("source_spans", [])
            if isinstance(span, dict) and str(span.get("span_id")) in span_aliases
        }),
    }


def _materialize_claim_assessment(
    assessment: DiagnosticClaimAssessment,
    case: dict[str, Any],
) -> dict[str, Any]:
    evidence_status = assessment.evidence_status
    reason = assessment.reason
    if evidence_status == "supported" and (
        not case.get("evidence_ids") or not case.get("source_span_ids")
    ):
        evidence_status = "unverifiable"
        reason = "The claim has no complete claim-owned evidence and source-span links."
    return {
        "claim_id": str(case["claim_id"]),
        "evidence_status": evidence_status,
        "paper_relevance": assessment.paper_relevance,
        "priority_rank": assessment.priority_rank,
        "reason": reason,
        "unsupported_assertions": assessment.unsupported_assertions,
        "evidence_ids": list(case.get("evidence_ids") or []),
        "source_span_ids": list(case.get("source_span_ids") or []),
    }


def _diagnostic_finding_rows(
    report: DiagnosticSubmissionReport,
    assessments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ineligible_claim_ids = {
        str(assessment["claim_id"])
        for assessment in assessments
        if assessment["evidence_status"] != "supported"
    }
    rows = [
        finding.model_dump(mode="json")
        for finding in report.findings
        if not (
            finding.dimension == "evidence_relevance"
            and str(finding.target_id or "") in ineligible_claim_ids
        )
    ]
    severity_by_status = {
        "partially_supported": "major",
        "unsupported": "critical",
        "unverifiable": "major",
    }
    for assessment in assessments:
        status = str(assessment["evidence_status"])
        if status == "supported":
            continue
        rows.append(
            {
                "dimension": "evidence_relevance",
                "severity": severity_by_status[status],
                "target_type": "claim",
                "target_id": assessment["claim_id"],
                "message": assessment["reason"],
                "evidence_span": None,
                "suggestion": "Provide claim-owned source evidence for every material assertion.",
                "metadata": {
                    "code": "claim_evidence_ineligible",
                    "evidence_status": status,
                    "evidence_ids": assessment["evidence_ids"],
                    "source_span_ids": assessment["source_span_ids"],
                    "unsupported_assertions": assessment["unsupported_assertions"],
                },
            }
        )
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_path(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in value
    )
    return safe[:180] or "unknown"
