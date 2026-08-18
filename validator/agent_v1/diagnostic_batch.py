from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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


class DiagnosticCandidateEvidence(_StrictModel):
    claim_id: str = Field(min_length=1)
    status: Literal["supported", "partially_supported", "unsupported", "unverifiable"]
    evidence_ids: list[str] = Field(default_factory=list)
    source_span_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    unsupported_assertions: list[str] = Field(default_factory=list)


class DiagnosticSubmissionReport(_StrictModel):
    submission_ref: str = Field(min_length=1)
    candidate_evidence: list[DiagnosticCandidateEvidence]
    findings: list[DiagnosticFinding] = Field(default_factory=list)


class DiagnosticBatchOutput(_StrictModel):
    reports: list[DiagnosticSubmissionReport]


@dataclass(frozen=True)
class DiagnosticBatchConfig:
    root: Path
    harness: str
    provider: str
    model: str
    command_template: str = ""
    batch_size: int = 1
    max_input_bytes: int = 8_000_000
    max_turns: int = 30
    max_tokens: int = 16384
    timeout_seconds: float = 1800.0
    output_poll_seconds: float = 0.5
    output_stable_seconds: float = 1.0
    usage_grace_seconds: float = 15.0
    repair_batch_size: int = 4
    repair_max_depth: int = 3
    repair_max_workers: int = 2

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
            max_input_bytes=max(
                0,
                int(os.getenv("CLAIMS_DIAGNOSTIC_BATCH_MAX_INPUT_BYTES", "8000000") or 0),
            ),
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
            repair_batch_size=max(
                1,
                int(os.getenv("CLAIMS_DIAGNOSTIC_REPAIR_BATCH_SIZE", "4") or 4),
            ),
            repair_max_depth=max(
                0,
                int(os.getenv("CLAIMS_DIAGNOSTIC_REPAIR_MAX_DEPTH", "3") or 0),
            ),
            repair_max_workers=max(
                1,
                int(os.getenv("CLAIMS_DIAGNOSTIC_REPAIR_MAX_WORKERS", "2") or 2),
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
    repair_operation_ids: list[str] = field(default_factory=list)
    repair_workspaces: list[Path] = field(default_factory=list)


def shard_diagnostic_submissions(
    submissions: list[DiagnosticBatchSubmission],
    *,
    max_count: int,
    max_input_bytes: int,
) -> list[list[DiagnosticBatchSubmission]]:
    shards: list[list[DiagnosticBatchSubmission]] = []
    current: list[DiagnosticBatchSubmission] = []
    current_bytes = 0
    current_source_hashes: set[str] = set()
    for submission in submissions:
        source_bytes = _json_bytes(submission.source_payload or {})
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        item_bytes = (
            len(_json_bytes(submission.artifact))
            + len(_json_bytes(submission.structural_findings))
            + len(_json_bytes(submission.grounding_findings))
            + (0 if source_hash in current_source_hashes else len(source_bytes))
        )
        count_full = len(current) >= max(1, max_count)
        bytes_full = bool(max_input_bytes and current and current_bytes + item_bytes > max_input_bytes)
        if count_full or bytes_full:
            shards.append(current)
            current = []
            current_bytes = 0
            current_source_hashes = set()
            item_bytes = (
                len(_json_bytes(submission.artifact))
                + len(_json_bytes(submission.structural_findings))
                + len(_json_bytes(submission.grounding_findings))
                + len(source_bytes)
            )
        current.append(submission)
        current_bytes += item_bytes
        current_source_hashes.add(source_hash)
    if current:
        shards.append(current)
    return shards


def run_diagnostic_batch(
    *,
    config: DiagnosticBatchConfig,
    run_id: str,
    paper_id: str,
    shard_index: int,
    submissions: list[DiagnosticBatchSubmission],
    _repair_depth: int = 0,
    _workspace_name: str = "",
) -> DiagnosticBatchExecution:
    workspace_name = _workspace_name or f"shard_{shard_index:04d}"
    operation_id = (
        f"{run_id}:{paper_id}:diagnostic-shard-{shard_index:04d}"
        if not _workspace_name
        else f"{run_id}:{paper_id}:diagnostic-{workspace_name.replace('_', '-')}"
    )
    root = (
        config.root
        / _safe_path(run_id)
        / _safe_path(paper_id)
        / workspace_name
    )
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    source_paths: dict[str, Path] = {}
    task_rows: list[dict[str, Any]] = []
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
        _write_json(artifact_path, submission.artifact)
        _write_json(structural_path, submission.structural_findings)
        _write_json(grounding_path, submission.grounding_findings)
        task_rows.append(
            {
                "submission_ref": submission.submission_ref,
                "artifact_path": str(artifact_path),
                "source_payload_path": str(source_path),
                "structural_findings_path": str(structural_path),
                "grounding_findings_path": str(grounding_path),
            }
        )

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
    output_path = root / "output.json"
    stdout_path = root / "stdout.log"
    stderr_path = root / "stderr.log"
    output_path.unlink(missing_ok=True)
    _write_json(
        task_path,
        {
            "paper_ref": "P0001",
            "submissions": task_rows,
            "requirements": {
                "review_every_submission_independently": True,
                "emit_exactly_one_report_per_submission_ref": True,
                "do_not_compare_rank_or_copy_findings_between_submissions": True,
                "use_only_each_submission_artifact_and_its_linked_source_payload": True,
            },
        },
    )
    _write_json(schema_path, DiagnosticBatchOutput.model_json_schema())

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
            "Run this Claims batched diagnostic validation task.",
            f"Read the complete skill instructions from {skill_path}.",
            f"Read the complete task from {task_path}.",
            f"Validate the result against {schema_path}.",
            f"Write exactly one JSON object to {output_path}.",
            "After writing valid output, finish immediately without another tool or model call.",
            "Do not modify the task, schema, source, or submission files.",
        ]
    )
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    completed = None
    error: str | None = None
    expected = {submission.submission_ref for submission in submissions}
    try:
        completed = _run_until_valid_output(
            [*command, query],
            cwd=root,
            output_path=output_path,
            output_model=DiagnosticBatchOutput,
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
        payload = _read_batch_output(output_path)
        partial_reports = _read_partial_batch_reports(output_path) if payload is None else []
        if payload is None and not partial_reports:
            stderr = completed.stderr[-1000:] if completed is not None else ""
            raise RuntimeError(f"Diagnostic batch did not produce valid output. {stderr}")
        reports: dict[str, dict[str, Any]] = {}
        duplicate_refs: set[str] = set()
        submissions_by_ref = {
            submission.submission_ref: submission for submission in submissions
        }
        for report in payload.reports if payload is not None else partial_reports:
            if report.submission_ref not in expected:
                continue
            if report.submission_ref in reports:
                duplicate_refs.add(report.submission_ref)
                continue
            submission = submissions_by_ref[report.submission_ref]
            if not _valid_candidate_evidence_partition(report, submission):
                continue
            reports[report.submission_ref] = {
                "candidate_evidence": [
                    {
                        **assessment.model_dump(mode="json"),
                        "coverage_eligible": assessment.status == "supported",
                    }
                    for assessment in report.candidate_evidence
                ],
                "findings": _diagnostic_finding_rows(report),
            }
        for duplicate_ref in duplicate_refs:
            reports.pop(duplicate_ref, None)
    except Exception as exc:
        reports = {}
        error = f"{type(exc).__name__}: {exc}"

    base_usage = (
        usage_from_cli_process(
            command,
            completed.stdout if completed is not None else "",
            completed.stderr if completed is not None else "",
            cwd=root,
            started_at=started_at,
            model=config.model,
        )
        if completed is not None
        else empty_usage("diagnostic_batch_not_started")
    )
    initial_error = error
    repair_executions: list[DiagnosticBatchExecution] = []
    missing_refs = expected.difference(reports)
    if missing_refs and _repair_depth < config.repair_max_depth:
        missing_submissions = [
            submission
            for submission in submissions
            if submission.submission_ref in missing_refs
        ]
        repair_shards = [
            missing_submissions[index : index + config.repair_batch_size]
            for index in range(0, len(missing_submissions), config.repair_batch_size)
        ]

        def run_repair(repair_index: int, repair_shard: list[DiagnosticBatchSubmission]):
            return run_diagnostic_batch(
                config=config,
                run_id=run_id,
                paper_id=paper_id,
                shard_index=shard_index,
                submissions=repair_shard,
                _repair_depth=_repair_depth + 1,
                _workspace_name=(
                    f"{workspace_name}_repair_d{_repair_depth + 1}_{repair_index:02d}"
                ),
            )

        worker_count = min(config.repair_max_workers, len(repair_shards))
        if worker_count > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(run_repair, index, repair_shard): index
                    for index, repair_shard in enumerate(repair_shards, start=1)
                }
                repair_executions = [future.result() for future in as_completed(futures)]
        else:
            repair_executions = [
                run_repair(index, repair_shard)
                for index, repair_shard in enumerate(repair_shards, start=1)
            ]
        for repair in repair_executions:
            reports.update(repair.reports)

    remaining_refs = expected.difference(reports)
    if remaining_refs:
        error = (
            "Diagnostic batch omitted required submissions after bounded repair; "
            f"missing={sorted(remaining_refs)} depth={_repair_depth}."
        )
    else:
        error = None
    duration = time.perf_counter() - started
    usage = merge_usage([base_usage, *[repair.usage for repair in repair_executions]])
    repair_operation_ids = [
        operation
        for repair in repair_executions
        for operation in [repair.operation_id, *repair.repair_operation_ids]
    ]
    repair_workspaces = [
        workspace
        for repair in repair_executions
        for workspace in [repair.workspace, *repair.repair_workspaces]
    ]
    _write_json(
        root / "manifest.json",
        {
            "schema": "claims_diagnostic_batch_v1",
            "operation_id": operation_id,
            "paper_id": paper_id,
            "submission_count": len(submissions),
            "completed_report_count": len(reports),
            "source_payload_count": len(source_paths),
            "harness": config.harness,
            "model": config.model,
            "duration_seconds": round(duration, 3),
            "status": "complete" if not error else "failed",
            "error": error,
            "initial_error": initial_error,
            "initial_completed_report_count": len(expected.difference(missing_refs)),
            "repair_operation_ids": repair_operation_ids,
            "repair_depth": _repair_depth,
        },
    )
    return DiagnosticBatchExecution(
        reports=reports,
        usage=usage,
        duration_seconds=duration,
        operation_id=operation_id,
        workspace=root,
        error=error,
        repair_operation_ids=repair_operation_ids,
        repair_workspaces=repair_workspaces,
    )


def precomputed_rigor_manifest(execution: DiagnosticBatchExecution) -> dict[str, Any]:
    return {
        "runtime": "diagnostic-file-batch",
        "elapsed_seconds": execution.duration_seconds,
        "usage": empty_usage("shared_diagnostic_batch"),
        "metadata": {
            "diagnostic_batch": True,
            "operation_id": execution.operation_id,
            "workspace": str(execution.workspace),
            "usage_recorded_separately": True,
            "repair_operation_ids": execution.repair_operation_ids,
            "repair_workspaces": [str(path) for path in execution.repair_workspaces],
        },
    }


def _read_batch_output(path: Path) -> DiagnosticBatchOutput | None:
    try:
        return DiagnosticBatchOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_partial_batch_reports(path: Path) -> list[DiagnosticSubmissionReport]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    records = payload.get("reports") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    reports: list[DiagnosticSubmissionReport] = []
    for record in records:
        try:
            reports.append(DiagnosticSubmissionReport.model_validate(record))
        except Exception:
            continue
    return reports


def _valid_candidate_evidence_partition(
    report: DiagnosticSubmissionReport,
    submission: DiagnosticBatchSubmission,
) -> bool:
    claims = _artifact_claims(submission.artifact)
    assessments = {assessment.claim_id: assessment for assessment in report.candidate_evidence}
    if len(assessments) != len(report.candidate_evidence) or set(assessments) != set(claims):
        return False
    for claim_id, assessment in assessments.items():
        claim = claims[claim_id]
        linked_evidence_ids = {
            str(item) for item in claim.get("evidence_ids", []) if str(item)
        }
        linked_span_ids = _claim_source_span_ids(claim)
        if not set(assessment.evidence_ids).issubset(linked_evidence_ids):
            return False
        if not set(assessment.source_span_ids).issubset(linked_span_ids):
            return False
        if assessment.status == "supported" and (
            not assessment.evidence_ids or not assessment.source_span_ids
        ):
            return False
    return True


def _artifact_claims(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    logic = artifact.get("logic") if isinstance(artifact, dict) else None
    rows = logic.get("claims") if isinstance(logic, dict) else None
    claims: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        claim_id = str(row.get("claim_id") or "")
        if claim_id:
            claims[claim_id] = row
    return claims


def _claim_source_span_ids(claim: dict[str, Any]) -> set[str]:
    span_ids = {
        str(item) for item in claim.get("source_span_ids", []) if str(item)
    }
    sources = claim.get("sources")
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        span_ids.update(
            str(item) for item in source.get("span_ids", []) if str(item)
        )
    return span_ids


def _diagnostic_finding_rows(report: DiagnosticSubmissionReport) -> list[dict[str, Any]]:
    ineligible_claim_ids = {
        assessment.claim_id
        for assessment in report.candidate_evidence
        if assessment.status != "supported"
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
    for assessment in report.candidate_evidence:
        if assessment.status == "supported":
            continue
        rows.append(
            {
                "dimension": "evidence_relevance",
                "severity": severity_by_status[assessment.status],
                "target_type": "claim",
                "target_id": assessment.claim_id,
                "message": assessment.reason,
                "evidence_span": None,
                "suggestion": "Provide source evidence that supports every material assertion in the claim.",
                "metadata": {
                    "code": "candidate_evidence_ineligible",
                    "evidence_status": assessment.status,
                    "coverage_eligible": False,
                    "evidence_ids": assessment.evidence_ids,
                    "source_span_ids": assessment.source_span_ids,
                    "unsupported_assertions": assessment.unsupported_assertions,
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
    safe = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
    return safe[:180] or "unknown"
