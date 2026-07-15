from __future__ import annotations

from typing import Any

from .artifact_models import Artifact


def validate_agent_artifact(artifact: Artifact) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not artifact.paper.paper_id:
        issues.append(_issue("paper.paper_id", "missing", "paper_id is required"))
    if not artifact.paper.title:
        issues.append(_issue("paper.title", "missing", "title is empty"))
    if not artifact.logic.claims:
        issues.append(_issue("logic.claims", "missing", "no ARA claims were produced"))
    issues.extend(_coverage_issues(artifact))
    experiment_ids = {experiment.experiment_id for experiment in artifact.logic.experiments}
    evidence_ids = {record.evidence_id for record in artifact.evidence.records}
    claim_ids = {claim.claim_id for claim in artifact.logic.claims}
    for claim in artifact.logic.claims:
        if not claim.statement:
            issues.append(_issue(f"logic.claims.{claim.claim_id}.statement", "missing", "claim statement is empty"))
        if not claim.conditions:
            issues.append(_issue(f"logic.claims.{claim.claim_id}.conditions", "missing", "claim conditions are empty"))
        if not claim.falsification_criteria:
            issues.append(_issue(f"logic.claims.{claim.claim_id}.falsification_criteria", "missing", "falsification criteria are empty"))
        for experiment_id in claim.proof:
            if experiment_id not in experiment_ids:
                issues.append(_issue(f"logic.claims.{claim.claim_id}.proof", "unresolved_ref", f"unknown experiment `{experiment_id}`"))
        for evidence_id in claim.evidence_ids:
            if evidence_id not in evidence_ids:
                issues.append(_issue(f"logic.claims.{claim.claim_id}.evidence_ids", "unresolved_ref", f"unknown evidence `{evidence_id}`"))
    for experiment in artifact.logic.experiments:
        for claim_id in experiment.verifies:
            if claim_id not in claim_ids:
                issues.append(_issue(f"logic.experiments.{experiment.experiment_id}.verifies", "unresolved_ref", f"unknown claim `{claim_id}`"))
        for evidence_id in experiment.evidence_ids:
            if evidence_id not in evidence_ids:
                issues.append(_issue(f"logic.experiments.{experiment.experiment_id}.evidence_ids", "unresolved_ref", f"unknown evidence `{evidence_id}`"))
    return issues


def _coverage_issues(artifact: Artifact) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    claim_count = len(artifact.logic.claims)
    evidence_count = len(artifact.evidence.records)
    summary_count = len([item for item in artifact.paper.claims_summary if item.strip()])
    expected_claims = min(3, summary_count) if summary_count >= 3 else summary_count

    if expected_claims and claim_count < expected_claims:
        issues.append(
            _issue(
                "logic.claims",
                "insufficient_coverage",
                f"expected at least {expected_claims} source-grounded claims based on paper.claims_summary, found {claim_count}",
            )
        )
    if claim_count >= 3 and evidence_count < claim_count:
        issues.append(
            _issue(
                "evidence.records",
                "insufficient_evidence_coverage",
                f"expected at least one evidence record per claim for multi-claim artifacts, found {evidence_count} records for {claim_count} claims",
            )
        )
    if claim_count > 1:
        evidence_sets = {tuple(sorted(claim.evidence_ids)) for claim in artifact.logic.claims}
        if len(evidence_sets) == 1:
            issues.append(
                _issue(
                    "logic.claims",
                    "collapsed_evidence_coverage",
                    "multiple claims all point to the same evidence set; split evidence records by distinct result or support basis",
                )
            )
    return issues


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}
