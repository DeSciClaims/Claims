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

from miner.agent_v1.runtime.usage import empty_usage, usage_from_cli_process

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
                "assess_every_claim_ref_exactly_once": True,
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
            "Run this Claims paper-level diagnostic validation task.",
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
    reports: dict[str, dict[str, Any]] = {}
    error: str | None = None
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
        payload = DiagnosticBatchOutput.model_validate_json(output_path.read_text(encoding="utf-8"))
        reports = _validated_reports(
            payload,
            expected_submission_refs={submission.submission_ref for submission in submissions},
            claim_cases_by_submission=claim_cases_by_submission,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    usage = (
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
    duration = time.perf_counter() - started
    _write_json(
        root / "manifest.json",
        {
            "schema": "claims_diagnostic_paper_v2",
            "operation_id": operation_id,
            "paper_id": paper_id,
            "submission_count": len(submissions),
            "claim_count": sum(len(rows) for rows in claim_cases_by_submission.values()),
            "completed_report_count": len(reports),
            "source_payload_count": len(source_paths),
            "harness": config.harness,
            "model": config.model,
            "duration_seconds": round(duration, 3),
            "status": "complete" if not error else "failed",
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
    )


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


def _validated_reports(
    payload: DiagnosticBatchOutput,
    *,
    expected_submission_refs: set[str],
    claim_cases_by_submission: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    refs = [report.submission_ref for report in payload.reports]
    if len(refs) != len(set(refs)) or set(refs) != expected_submission_refs:
        raise ValueError(
            "Diagnostic output must contain exactly one report per submission; "
            f"expected={sorted(expected_submission_refs)} actual={sorted(refs)}"
        )
    reports: dict[str, dict[str, Any]] = {}
    for report in payload.reports:
        cases = claim_cases_by_submission[report.submission_ref]
        assessment_refs = [assessment.claim_ref for assessment in report.claim_assessments]
        if len(assessment_refs) != len(set(assessment_refs)) or set(assessment_refs) != set(cases):
            raise ValueError(
                "Diagnostic output must assess every claim exactly once; "
                f"submission={report.submission_ref} expected={sorted(cases)} "
                f"actual={sorted(assessment_refs)}"
            )
        assessments = [
            _materialize_claim_assessment(assessment, cases[assessment.claim_ref])
            for assessment in report.claim_assessments
        ]
        reports[report.submission_ref] = {
            "claim_assessments": assessments,
            "findings": _diagnostic_finding_rows(report, assessments),
        }
    return reports


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
