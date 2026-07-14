from __future__ import annotations

from typing import Any

from .models import AraArtifact


def validate_ara_artifact(artifact: AraArtifact) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not artifact.paper.paper_id:
        issues.append(_issue("paper.paper_id", "missing", "paper_id is required"))
    if not artifact.paper.title:
        issues.append(_issue("paper.title", "missing", "title is empty"))
    if not artifact.logic.claims:
        issues.append(_issue("logic.claims", "missing", "no ARA claims were produced"))
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


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}
