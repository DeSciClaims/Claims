from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from miner.agent_v1.artifact_models import Artifact

from .models import AgentV1ValidationFinding


def run_structural_checks(artifact_path: Path) -> tuple[dict[str, Any], Artifact | None, list[AgentV1ValidationFinding]]:
    findings: list[AgentV1ValidationFinding] = []
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, None, [
            _finding(
                "blocker",
                "json_parse",
                f"Artifact is not valid JSON: {exc}",
                code="invalid_json",
                target_type="artifact",
            )
        ]
    if not isinstance(raw, dict):
        return {}, None, [_finding("blocker", "json_parse", "Artifact JSON must be an object.", code="invalid_json")]

    missing_layers = [key for key in ("paper", "logic", "evidence", "trace", "src", "metadata") if key not in raw]
    for key in missing_layers:
        findings.append(
            _finding(
                "blocker",
                "required_layers",
                f"Missing required top-level agent artifact layer: {key}.",
                target_type="layer",
                target_id=key,
                code="missing_layer",
            )
        )

    artifact = None
    try:
        artifact = Artifact.model_validate(raw)
    except Exception as exc:
        findings.append(
            _finding(
                "blocker" if missing_layers else "critical",
                "schema_validation",
                f"Artifact does not validate as Artifact: {exc}",
                code="schema_validation_failed",
                target_type="artifact",
            )
        )
        if missing_layers:
            return raw, None, _number_findings(findings)
        try:
            artifact = Artifact.model_validate(_repair_minimal_defaults(raw))
        except Exception:
            return raw, None, _number_findings(findings)

    findings.extend(_check_unique_ids("claim", [c.claim_id for c in artifact.logic.claims]))
    findings.extend(_check_unique_ids("concept", [c.concept_id for c in artifact.logic.concepts]))
    findings.extend(_check_unique_ids("experiment", [e.experiment_id for e in artifact.logic.experiments]))
    findings.extend(_check_unique_ids("evidence", [e.evidence_id for e in artifact.evidence.records]))
    trace_ids: list[str] = []
    _collect_trace_ids(artifact.trace, trace_ids)
    findings.extend(_check_unique_ids("trace_node", trace_ids))

    claim_ids = {c.claim_id for c in artifact.logic.claims}
    experiment_ids = {e.experiment_id for e in artifact.logic.experiments}
    evidence_ids = {e.evidence_id for e in artifact.evidence.records}

    if not artifact.logic.claims:
        findings.append(_finding("critical", "coverage", "Artifact contains no claims.", code="no_claims", target_type="logic"))
    if not artifact.evidence.records:
        findings.append(
            _finding("critical", "coverage", "Artifact contains no evidence records.", code="no_evidence_records", target_type="evidence")
        )

    for claim in artifact.logic.claims:
        if not claim.statement.strip():
            findings.append(_missing_field("claim", claim.claim_id, "statement", "critical"))
        if not claim.conditions.strip():
            findings.append(_missing_field("claim", claim.claim_id, "conditions", "major"))
        if not claim.falsification_criteria.strip():
            findings.append(_missing_field("claim", claim.claim_id, "falsification_criteria", "major"))
        for evidence_id in claim.evidence_ids:
            if evidence_id not in evidence_ids:
                findings.append(_unresolved("claim", claim.claim_id, "evidence_id", evidence_id, "critical"))
        for experiment_id in claim.proof:
            if experiment_id not in experiment_ids:
                findings.append(_unresolved("claim", claim.claim_id, "proof", experiment_id, "critical"))
        for dependency_id in claim.dependencies:
            if dependency_id not in claim_ids:
                findings.append(_unresolved("claim", claim.claim_id, "dependency", dependency_id, "major"))

    for experiment in artifact.logic.experiments:
        for claim_id in experiment.verifies:
            if claim_id not in claim_ids:
                findings.append(_unresolved("experiment", experiment.experiment_id, "verifies", claim_id, "critical"))
        for evidence_id in experiment.evidence_ids:
            if evidence_id not in evidence_ids:
                findings.append(_unresolved("experiment", experiment.experiment_id, "evidence_id", evidence_id, "major"))
        for field_name in ("setup", "procedure", "expected_outcome"):
            if not str(getattr(experiment, field_name)).strip():
                findings.append(_missing_field("experiment", experiment.experiment_id, field_name, "major"))

    for evidence in artifact.evidence.records:
        if not evidence.summary.strip():
            findings.append(_missing_field("evidence", evidence.evidence_id, "summary", "critical"))
        for claim_id in evidence.linked_claim_ids:
            if claim_id not in claim_ids:
                findings.append(_unresolved("evidence", evidence.evidence_id, "linked_claim_id", claim_id, "critical"))

    trace_reference_ids = claim_ids | experiment_ids | evidence_ids | {c.concept_id for c in artifact.logic.concepts}
    _check_trace_refs(artifact.trace, trace_reference_ids, findings)
    return raw, artifact, _number_findings(findings)


def _repair_minimal_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    repaired = dict(raw)
    repaired.setdefault("metadata", {})
    repaired.setdefault("ara_version", "1.0")
    return repaired


def _check_unique_ids(target_type: str, ids: list[str]) -> list[AgentV1ValidationFinding]:
    counts = Counter(ids)
    return [
        _finding(
            "critical",
            "id_uniqueness",
            f"Duplicate {target_type} id: {item_id}.",
            target_type=target_type,
            target_id=item_id,
            code="duplicate_id",
        )
        for item_id, count in counts.items()
        if item_id and count > 1
    ]


def _collect_trace_ids(node, ids: list[str]) -> None:
    ids.append(node.node_id)
    for child in node.children:
        _collect_trace_ids(child, ids)


def _check_trace_refs(node, valid_reference_ids: set[str], findings: list[AgentV1ValidationFinding]) -> None:
    for evidence_id in node.evidence:
        if evidence_id not in valid_reference_ids:
            findings.append(_unresolved("trace_node", node.node_id, "evidence", evidence_id, "major"))
    for child in node.children:
        _check_trace_refs(child, valid_reference_ids, findings)


def _missing_field(target_type: str, target_id: str, field_name: str, severity: str) -> AgentV1ValidationFinding:
    return _finding(
        severity,
        "required_field",
        f"{target_type} {target_id} has an empty required field: {field_name}.",
        target_type=target_type,
        target_id=target_id,
        code="missing_required_field",
        field=field_name,
    )


def _unresolved(target_type: str, target_id: str, field_name: str, ref_id: str, severity: str) -> AgentV1ValidationFinding:
    return _finding(
        severity,
        "cross_reference_resolution",
        f"{target_type} {target_id} references missing {field_name}: {ref_id}.",
        target_type=target_type,
        target_id=target_id,
        code="unresolved_reference",
        field=field_name,
        ref_id=ref_id,
    )


def _finding(severity: str, dimension: str, message: str, **metadata) -> AgentV1ValidationFinding:
    return AgentV1ValidationFinding(
        pass_name="structural",
        dimension=dimension,
        severity=severity,  # type: ignore[arg-type]
        target_type=metadata.pop("target_type", None),
        target_id=metadata.pop("target_id", None),
        message=message,
        suggestion=_suggestion_for_code(str(metadata.get("code", ""))),
        metadata=metadata,
    )


def _suggestion_for_code(code: str) -> str | None:
    return {
        "invalid_json": "Return strict JSON matching the Claims agent schema.",
        "schema_validation_failed": "Regenerate or repair the artifact against agent_schema.json.",
        "missing_layer": "Include every required agent artifact top-level layer.",
        "duplicate_id": "Use stable unique IDs within each artifact layer.",
        "unresolved_reference": "Update the reference or add the missing target object.",
        "missing_required_field": "Populate the field with source-bounded content.",
        "no_evidence_records": "Add evidence records grounded in source refs.",
    }.get(code)


def _number_findings(findings: list[AgentV1ValidationFinding]) -> list[AgentV1ValidationFinding]:
    for index, finding in enumerate(findings, start=1):
        finding.finding_id = f"S{index:03d}"
    return findings
