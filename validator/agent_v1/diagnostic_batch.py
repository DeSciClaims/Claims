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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    max_claims: int = 80
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
            max_claims=max(
                0,
                int(os.getenv("CLAIMS_DIAGNOSTIC_BATCH_MAX_CLAIMS", "80") or 0),
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
    report_rejections: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _AliasedDiagnosticSubmission:
    original: DiagnosticBatchSubmission
    model: DiagnosticBatchSubmission
    alias_to_original: dict[str, str]


def shard_diagnostic_submissions(
    submissions: list[DiagnosticBatchSubmission],
    *,
    max_count: int,
    max_input_bytes: int,
    max_claims: int = 0,
) -> list[list[DiagnosticBatchSubmission]]:
    shards: list[list[DiagnosticBatchSubmission]] = []
    current: list[DiagnosticBatchSubmission] = []
    current_bytes = 0
    current_claims = 0
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
        item_claims = len(_artifact_claims(submission.artifact))
        count_full = len(current) >= max(1, max_count)
        bytes_full = bool(max_input_bytes and current and current_bytes + item_bytes > max_input_bytes)
        claims_full = bool(max_claims and current and current_claims + item_claims > max_claims)
        if count_full or bytes_full or claims_full:
            shards.append(current)
            current = []
            current_bytes = 0
            current_claims = 0
            current_source_hashes = set()
            item_bytes = (
                len(_json_bytes(submission.artifact))
                + len(_json_bytes(submission.structural_findings))
                + len(_json_bytes(submission.grounding_findings))
                + len(source_bytes)
            )
        current.append(submission)
        current_bytes += item_bytes
        current_claims += item_claims
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
    _repair_rejections: dict[str, list[str]] | None = None,
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
    aliased_submissions = [
        _alias_diagnostic_submission(submission, index)
        for index, submission in enumerate(submissions)
    ]
    aliases_by_ref = {
        aliased.model.submission_ref: aliased for aliased in aliased_submissions
    }
    source_paths: dict[str, Path] = {}
    task_rows: list[dict[str, Any]] = []
    for aliased in aliased_submissions:
        submission = aliased.model
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
                "required_claim_refs": sorted(
                    _artifact_claims(submission.artifact),
                    key=_short_identifier_sort_key,
                ),
                "validator_rejections": list(
                    (_repair_rejections or {}).get(aliased.original.submission_ref, [])
                ),
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
                "emit_exactly_one_candidate_evidence_row_per_required_claim_ref": True,
                "do_not_compare_rank_or_copy_findings_between_submissions": True,
                "use_only_each_submission_artifact_and_its_linked_source_payload": True,
                "use_only_short_identifiers_from_the_model_facing_files": True,
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
    expected_model_refs = set(aliases_by_ref)
    report_rejections: dict[str, list[str]] = {}
    initial_completed_report_count = 0
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
        partial_reports, partial_errors = (
            _read_partial_batch_reports_with_errors(output_path)
            if payload is None
            else ([], {})
        )
        for model_ref, reasons in partial_errors.items():
            aliased = aliases_by_ref.get(model_ref)
            if aliased is not None:
                report_rejections.setdefault(aliased.original.submission_ref, []).extend(
                    reasons
                )
        if payload is None and not partial_reports:
            stderr = completed.stderr[-1000:] if completed is not None else ""
            raise RuntimeError(f"Diagnostic batch did not produce valid output. {stderr}")
        reports: dict[str, dict[str, Any]] = {}
        duplicate_refs: set[str] = set()
        seen_model_refs: set[str] = set()
        candidate_reports = payload.reports if payload is not None else partial_reports
        for report in candidate_reports:
            aliased = aliases_by_ref.get(report.submission_ref)
            if aliased is None:
                continue
            if report.submission_ref in seen_model_refs:
                duplicate_refs.add(report.submission_ref)
                continue
            seen_model_refs.add(report.submission_ref)
            report = _attach_authoritative_candidate_links(report, aliased.model)
            partition_errors = _candidate_evidence_partition_errors(
                report,
                aliased.model,
            )
            if partition_errors:
                report_rejections.setdefault(aliased.original.submission_ref, []).extend(
                    partition_errors
                )
                continue
            restored = _restore_diagnostic_report(report, aliased)
            reports[aliased.original.submission_ref] = {
                "candidate_evidence": [
                    {
                        **assessment.model_dump(mode="json"),
                        "coverage_eligible": assessment.status == "supported",
                    }
                    for assessment in restored.candidate_evidence
                ],
                "findings": _diagnostic_finding_rows(restored),
            }
        for duplicate_model_ref in duplicate_refs:
            aliased = aliases_by_ref[duplicate_model_ref]
            reports.pop(aliased.original.submission_ref, None)
            report_rejections.setdefault(aliased.original.submission_ref, []).append(
                f"Duplicate report for submission ref {duplicate_model_ref}."
            )
        omitted_model_refs = expected_model_refs.difference(seen_model_refs)
        for model_ref in omitted_model_refs:
            original_ref = aliases_by_ref[model_ref].original.submission_ref
            report_rejections.setdefault(original_ref, []).append(
                f"Missing report for submission ref {model_ref}."
            )
        initial_completed_report_count = len(reports)
    except Exception as exc:
        reports = {}
        error = f"{type(exc).__name__}: {exc}"
        initial_completed_report_count = 0

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
        repair_size = min(
            config.repair_batch_size,
            max(1, (len(missing_submissions) + 1) // 2),
        )
        candidate_repair_shards = [
            missing_submissions[index : index + repair_size]
            for index in range(0, len(missing_submissions), repair_size)
        ]
        # A singleton is the individual fallback. Do not run it once here and
        # then run the legacy per-miner validator again downstream if it fails.
        # Shared-context repair stops at two submissions; unresolved singleton
        # work is attempted exactly once by the established individual path.
        repair_shards = [shard for shard in candidate_repair_shards if len(shard) > 1]

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
                _repair_rejections={
                    submission.submission_ref: report_rejections.get(
                        submission.submission_ref,
                        ["The previous attempt omitted this submission or returned invalid output."],
                    )
                    for submission in repair_shard
                },
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
            for submission_ref, reasons in repair.report_rejections.items():
                report_rejections.setdefault(submission_ref, []).extend(reasons)

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
            "initial_completed_report_count": initial_completed_report_count,
            "repair_operation_ids": repair_operation_ids,
            "repair_depth": _repair_depth,
            "report_rejections": report_rejections,
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
        report_rejections=report_rejections,
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
            "report_rejection_count": sum(
                len(reasons) for reasons in execution.report_rejections.values()
            ),
        },
    }


def _read_batch_output(path: Path) -> DiagnosticBatchOutput | None:
    try:
        return DiagnosticBatchOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_partial_batch_reports(path: Path) -> list[DiagnosticSubmissionReport]:
    reports, _errors = _read_partial_batch_reports_with_errors(path)
    return reports


def _read_partial_batch_reports_with_errors(
    path: Path,
) -> tuple[list[DiagnosticSubmissionReport], dict[str, list[str]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], {"": ["Diagnostic output is not valid JSON."]}
    records = payload.get("reports") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return [], {"": ["Diagnostic output must contain a reports array."]}
    reports: list[DiagnosticSubmissionReport] = []
    errors: dict[str, list[str]] = {}
    for record in records:
        try:
            reports.append(DiagnosticSubmissionReport.model_validate(record))
        except ValidationError as exc:
            model_ref = str(record.get("submission_ref") or "") if isinstance(record, dict) else ""
            errors.setdefault(model_ref, []).extend(
                _format_validation_error(error) for error in exc.errors(include_url=False)
            )
    return reports, errors


def _format_validation_error(error: dict[str, Any]) -> str:
    location = ".".join(str(item) for item in error.get("loc", [])) or "output"
    return f"{location}: {error.get('msg') or error.get('type') or 'invalid value'}"


def _alias_diagnostic_submission(
    submission: DiagnosticBatchSubmission,
    index: int,
) -> _AliasedDiagnosticSubmission:
    claim_ids = list(_artifact_claims(submission.artifact))
    evidence_ids = _artifact_evidence_ids(submission.artifact)
    span_ids = _source_span_ids(submission.artifact, submission.source_payload)
    alias_by_original: dict[str, str] = {}
    for prefix, identifiers in (
        ("c", claim_ids),
        ("e", evidence_ids),
        ("p", span_ids),
    ):
        for item_index, identifier in enumerate(identifiers):
            alias_by_original.setdefault(identifier, f"{prefix}{item_index}")
    alias_to_original = {
        alias: original for original, alias in alias_by_original.items()
    }
    model_ref = f"s{index}"
    return _AliasedDiagnosticSubmission(
        original=submission,
        model=DiagnosticBatchSubmission(
            submission_ref=model_ref,
            artifact=_replace_exact_identifiers(submission.artifact, alias_by_original),
            source_payload=_replace_exact_identifiers(
                submission.source_payload,
                alias_by_original,
            ),
            structural_findings=_replace_exact_identifiers(
                submission.structural_findings,
                alias_by_original,
            ),
            grounding_findings=_replace_exact_identifiers(
                submission.grounding_findings,
                alias_by_original,
            ),
        ),
        alias_to_original=alias_to_original,
    )


def _restore_diagnostic_report(
    report: DiagnosticSubmissionReport,
    aliased: _AliasedDiagnosticSubmission,
) -> DiagnosticSubmissionReport:
    restored = _replace_exact_identifiers(
        report.model_dump(mode="json"),
        aliased.alias_to_original,
    )
    restored["submission_ref"] = aliased.original.submission_ref
    return DiagnosticSubmissionReport.model_validate(restored)


def _attach_authoritative_candidate_links(
    report: DiagnosticSubmissionReport,
    submission: DiagnosticBatchSubmission,
) -> DiagnosticSubmissionReport:
    claims = _artifact_claims(submission.artifact)
    assessments: list[DiagnosticCandidateEvidence] = []
    for assessment in report.candidate_evidence:
        claim = claims.get(assessment.claim_id)
        if claim is None:
            assessments.append(assessment)
            continue
        assessments.append(
            assessment.model_copy(
                update={
                    "evidence_ids": sorted(
                        {str(item) for item in claim.get("evidence_ids", []) if str(item)},
                        key=_short_identifier_sort_key,
                    ),
                    "source_span_ids": sorted(
                        _claim_source_span_ids(claim),
                        key=_short_identifier_sort_key,
                    ),
                }
            )
        )
    return report.model_copy(update={"candidate_evidence": assessments})


def _replace_exact_identifiers(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_exact_identifiers(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            replacements.get(str(key), str(key)): _replace_exact_identifiers(item, replacements)
            for key, item in value.items()
        }
    return value


def _artifact_evidence_ids(artifact: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    evidence = artifact.get("evidence") if isinstance(artifact, dict) else None
    if isinstance(evidence, dict):
        for collection in ("records", "items", "evidence_items"):
            rows = evidence.get(collection)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                identifier = str(row.get("evidence_id") or row.get("id") or "")
                if identifier and identifier not in identifiers:
                    identifiers.append(identifier)
    for identifier in _strings_for_keys(artifact, {"evidence_ids"}):
        if identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def _source_span_ids(
    artifact: dict[str, Any],
    source_payload: dict[str, Any] | None,
) -> list[str]:
    identifiers: list[str] = []
    for payload in (source_payload or {}, artifact):
        for identifier in _strings_for_keys(
            payload,
            {"span_id", "span_ids", "source_span_ids"},
        ):
            if identifier not in identifiers:
                identifiers.append(identifier)
    return identifiers


def _strings_for_keys(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                values = item if isinstance(item, list) else [item]
                found.extend(str(candidate) for candidate in values if str(candidate))
            found.extend(_strings_for_keys(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_strings_for_keys(item, keys))
    return found


def _short_identifier_sort_key(value: str) -> tuple[str, int | str]:
    prefix = value[:1]
    suffix = value[1:]
    return (prefix, int(suffix)) if suffix.isdigit() else (prefix, value)


def _valid_candidate_evidence_partition(
    report: DiagnosticSubmissionReport,
    submission: DiagnosticBatchSubmission,
) -> bool:
    return not _candidate_evidence_partition_errors(report, submission)


def _candidate_evidence_partition_errors(
    report: DiagnosticSubmissionReport,
    submission: DiagnosticBatchSubmission,
) -> list[str]:
    claims = _artifact_claims(submission.artifact)
    assessments = {assessment.claim_id: assessment for assessment in report.candidate_evidence}
    errors: list[str] = []
    duplicate_claim_ids = sorted(
        {
            assessment.claim_id
            for assessment in report.candidate_evidence
            if sum(
                item.claim_id == assessment.claim_id
                for item in report.candidate_evidence
            )
            > 1
        },
        key=_short_identifier_sort_key,
    )
    missing_claim_ids = sorted(
        set(claims).difference(assessments),
        key=_short_identifier_sort_key,
    )
    unknown_claim_ids = sorted(
        set(assessments).difference(claims),
        key=_short_identifier_sort_key,
    )
    if duplicate_claim_ids:
        errors.append(f"Duplicate candidate evidence claim refs: {duplicate_claim_ids}.")
    if missing_claim_ids:
        errors.append(f"Missing candidate evidence claim refs: {missing_claim_ids}.")
    if unknown_claim_ids:
        errors.append(f"Unknown candidate evidence claim refs: {unknown_claim_ids}.")
    for claim_id, assessment in assessments.items():
        if claim_id not in claims:
            continue
        claim = claims[claim_id]
        linked_evidence_ids = {
            str(item) for item in claim.get("evidence_ids", []) if str(item)
        }
        linked_span_ids = _claim_source_span_ids(claim)
        unknown_evidence_ids = sorted(set(assessment.evidence_ids).difference(linked_evidence_ids))
        unknown_span_ids = sorted(set(assessment.source_span_ids).difference(linked_span_ids))
        if unknown_evidence_ids:
            errors.append(
                f"Claim {claim_id} cites unlinked evidence refs: {unknown_evidence_ids}."
            )
        if unknown_span_ids:
            errors.append(
                f"Claim {claim_id} cites unlinked source span refs: {unknown_span_ids}."
            )
        if assessment.status == "supported" and (
            not assessment.evidence_ids or not assessment.source_span_ids
        ):
            errors.append(
                f"Claim {claim_id} is marked supported without both evidence and source span refs."
            )
    return errors


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
