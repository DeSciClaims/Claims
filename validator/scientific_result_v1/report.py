"""Per-rule decisions with explicit uncertainty and fail-closed readiness."""

from __future__ import annotations
from datetime import datetime, timezone
from .registry import rules

STATUSES = {"pass", "fail", "review_required", "not_checked", "not_applicable"}


def combine(statuses):
    for status in ("fail", "review_required", "not_checked"):
        if status in statuses:
            return status
    return "pass" if "pass" in statuses else "not_applicable"


def build_report(
    *,
    payload,
    issues,
    reviews,
    source_hash,
    package_hash,
    schema_hash,
    scope,
    review_errors,
):
    registry = rules()
    document = payload if isinstance(payload, dict) else {}
    extraction = (
        document.get("extraction")
        if isinstance(document.get("extraction"), dict)
        else {}
    )
    coverage = (
        extraction.get("table_coverage")
        if isinstance(extraction.get("table_coverage"), dict)
        else {}
    )
    records = (
        document.get("results") if isinstance(document.get("results"), list) else []
    )
    records = [r for r in records if isinstance(r, dict)]
    structure = [i for i in issues if i["code"] == "schema"]
    checks = []
    for code, rule in registry.items():
        local = [i for i in issues if i["code"] == code]
        hard = [i for i in local if i.get("severity") != "review_required"]
        semantic = [r for r in reviews if r["rule_id"] == code]
        if structure:
            status = "not_checked"
        elif hard:
            status = "fail"
        elif any(i.get("blocking") for i in local):
            status = "review_required"
        elif rule["requires_source_review"]:
            status = (
                combine([r["status"] for r in semantic]) if semantic else "not_checked"
            )
            if local and status == "not_applicable":
                status = "review_required"
        else:
            status = "review_required" if local else "pass"
        checks.append(
            {
                "rule_id": code,
                "kind": rule["kind"],
                "requirement": rule["check"],
                "status": status,
                "not_checked_reason": (
                    "Blocked by structural errors"
                    if structure
                    else "A source review did not complete for this requirement"
                )
                if status == "not_checked"
                else None,
                "deterministic_findings": local,
                "source_reviews": semantic,
            }
        )
    extra = [i for i in issues if i["code"] not in registry and i["code"] != "schema"]
    hard_extra = any(i.get("severity") != "review_required" for i in extra)
    package_valid = (
        not structure
        and not hard_extra
        and not any(i.get("severity") != "review_required" for i in issues)
    )
    scientific = combine(
        [
            c["status"]
            for c in checks
            if registry[c["rule_id"]]["requires_source_review"]
        ]
    )
    complete = coverage.get("status") == "complete"
    closed = all(
        isinstance(r.get("closure"), dict) and r["closure"].get("status") == "closed"
        for r in records
    )
    ready = (
        package_valid
        and complete
        and closed
        and not review_errors
        and all(c["status"] in {"pass", "not_applicable"} for c in checks)
    )
    return {
        "schema_version": "claims_scientific_validation_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validation_scope": scope,
        "structurally_valid": not structure,
        "package_consistent": package_valid,
        "scientific_review_status": scientific,
        "submission_ready": ready,
        "decision": "accept"
        if ready
        else "reject"
        if structure or hard_extra or any(c["status"] == "fail" for c in checks)
        else "review_required",
        "execution_status": "error" if review_errors else "completed",
        "completion_requirements": {
            "table_coverage_complete": complete,
            "local_closures_closed": closed,
            "all_rules_resolved": all(
                c["status"] in {"pass", "not_applicable"} for c in checks
            ),
            "review_execution_completed": not review_errors
            and all(c["status"] != "not_checked" for c in checks),
        },
        "package_sha256": package_hash,
        "source_bundle_sha256": source_hash,
        "schema_sha256": schema_hash,
        "review_required_results": [
            r.get("id")
            for r in records
            if not isinstance(r.get("closure"), dict)
            or r["closure"].get("status") != "closed"
        ],
        "structural_findings": structure,
        "additional_findings": extra,
        "rules": checks,
        "review_errors": review_errors,
        "limitations": [
            "Source reviews are evidence-backed model judgments, not mathematical proofs.",
            "A miner-provided report or source-coverage claim is not an authoritative validation decision.",
        ],
    }
