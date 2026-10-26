"""Bind final code-review decisions to sealed packages and worker evidence.

Static code inspection cannot prove that a package lacks a hidden branch or
paper-keyed answer table. This module produces reproducible evidence and
requires explicit review decisions for those checks; it never infers a pass
from the absence of a simple string pattern.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from statistics import median
from typing import Any, Callable, Mapping

from miner.agent_v1.artifact_models import Artifact

from .duplicates import python_source_fingerprint
from .final_stage import effective_final_stage
from .publication import canonical_sha256


REVIEW_CHECKS = frozenset({"hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation"})
AUDIT_SCHEMA = "claims_final_code_audit_v1"


def _claim_statements(artifact: Mapping[str, Any], paper_id: str) -> tuple[int, str]:
    parsed = Artifact.model_validate(artifact)
    if parsed.paper.paper_id != paper_id:
        raise ValueError("audit artifact belongs to another paper")
    statements = sorted(" ".join(claim.statement.casefold().split())
                        for claim in parsed.logic.claims)
    return len(statements), canonical_sha256(statements)


def collect_final_code_evidence(
    round_id: str, *, backend: Any, fetch_package: Callable[[Mapping[str, Any]], bytes],
) -> dict[str, Any]:
    """Read frozen final jobs and package bytes without executing miner code."""
    stages = backend.list_stages(round_id)
    stage = effective_final_stage(stages)
    receipts = [row for row in stages if row.get("stage") == stage]
    if len(receipts) != 1:
        raise ValueError("code audit has no unique final stage")
    entries = receipts[0].get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("code audit has no final entrants")
    expected = {row["hotkey"]: row for row in entries}
    challengers = [row for row in entries if row.get("role") == "challenger"]
    baselines = [row for row in entries if row.get("role") in {"incumbent", "reference"}]
    if len(expected) != len(entries) or not challengers or len(baselines) != 1:
        raise ValueError("code audit final entrants are malformed")
    sample = [row for row in backend.list_sample(round_id) if row.get("stage") == stage]
    paper_ids = [row["paper_id"] for row in sample]
    if not paper_ids or len(set(paper_ids)) != len(paper_ids):
        raise ValueError("code audit final sample is empty or repeated")
    jobs_by_hotkey: dict[str, list[dict[str, Any]]] = {key: [] for key in expected}
    for paper_id in paper_ids:
        jobs = backend.list_paper_jobs(round_id, stage, paper_id)
        if len(jobs) != len(expected) or {row.get("hotkey") for row in jobs} != set(expected):
            raise ValueError("code audit final job grid is incomplete")
        for job in jobs:
            hotkey = job["hotkey"]
            if (job.get("status") != "completed" or job.get("round_id") != round_id
                or job.get("stage") != stage or job.get("paper_id") != paper_id
                or job.get("package_sha256") != expected[hotkey].get("package_sha256")
                or not isinstance(job.get("result_private"), dict)):
                raise ValueError("code audit job differs from frozen final")
            jobs_by_hotkey[hotkey].append(job)
    baseline_claims = []
    for job in jobs_by_hotkey[baselines[0]["hotkey"]]:
        result = job["result_private"]
        if result.get("status") == "completed":
            artifact = result.get("artifact")
            if not isinstance(artifact, dict):
                raise ValueError("successful baseline audit job has no artifact")
            baseline_claims.append(_claim_statements(artifact, job["paper_id"])[0])
    packages = []
    for entry in sorted(challengers, key=lambda row: row["hotkey"]):
        hotkey = entry["hotkey"]
        package_hash = entry["package_sha256"]
        package = fetch_package({
            "round_id": round_id, "stage": stage, "paper_id": paper_ids[0],
            "hotkey": hotkey, "package_sha256": package_hash,
        })
        if not isinstance(package, bytes) or hashlib.sha256(package).hexdigest() != package_hash:
            raise ValueError("code audit package differs from frozen final")
        try:
            fingerprint = python_source_fingerprint(package, max_source_bytes=100_000_000)
            source_scan_error = None
        except ValueError as exc:
            # A malformed finalist archive must fail that finalist's build
            # check, not prevent every other finalist from being audited.
            fingerprint = None
            source_scan_error = str(exc)
        statuses: Counter[str] = Counter()
        claims: list[int] = []
        statements: Counter[str] = Counter()
        caps = 0
        denied = 0
        total_tokens = 0
        for job in jobs_by_hotkey[hotkey]:
            result = job["result_private"]
            status = result.get("status")
            limit = result.get("token_limit")
            tokens = result.get("prompt_and_completion_tokens")
            packets = result.get("denied_egress_packets")
            if (not isinstance(status, str)
                or type(limit) is not int or limit < 1
                or type(tokens) is not int or tokens < 0
                or type(packets) is not int or packets < 0):
                raise ValueError("code audit job lacks trusted gateway or firewall usage")
            statuses[status] += 1
            total_tokens += tokens
            denied += packets
            caps += int(status == "cap_exceeded")
            if status == "completed":
                artifact = result.get("artifact")
                if not isinstance(artifact, dict):
                    raise ValueError("successful audit job has no artifact")
                count, digest = _claim_statements(artifact, job["paper_id"])
                claims.append(count)
                statements[digest] += 1
        packages.append({
            "hotkey": hotkey,
            "package_sha256": package_hash,
            "source_fingerprint": fingerprint,
            "source_scan_error": source_scan_error,
            "final_job_count": len(jobs_by_hotkey[hotkey]),
            "status_counts": dict(sorted(statuses.items())),
            "build_passed": bool(fingerprint and claims and not statuses["build_failed"]),
            "cap_trip_count": caps,
            "denied_egress_packets": denied,
            "measured_worker_tokens": total_tokens,
            "completed_claim_count_median": median(claims) if claims else None,
            "baseline_claim_count_median": median(baseline_claims) if baseline_claims else None,
            "max_same_statement_set_papers": max(statements.values(), default=0),
        })
    return {
        "schema_version": AUDIT_SCHEMA,
        "round_id": round_id,
        "stage": stage,
        "sample_sha256": canonical_sha256(sample),
        "packages": packages,
    }


def finalize_code_audit(
    evidence: Mapping[str, Any], review: Mapping[str, Any],
) -> dict[str, Any]:
    """Require explicit review of non-provable checks, bound to fresh evidence."""
    if (review.get("round_id") != evidence.get("round_id")
        or review.get("evidence_sha256") != canonical_sha256(evidence)
        or not isinstance(review.get("reviewer"), str) or not review["reviewer"]):
        raise ValueError("code audit review does not bind current evidence")
    decisions = review.get("packages")
    if not isinstance(decisions, list) or len(decisions) != len(evidence["packages"]):
        raise ValueError("code audit review does not cover finalists")
    by_hotkey = {row.get("hotkey"): row for row in decisions if isinstance(row, dict)}
    if len(by_hotkey) != len(decisions):
        raise ValueError("code audit review repeats a finalist")
    packages = []
    for row in evidence["packages"]:
        decision = by_hotkey.get(row["hotkey"])
        if (not isinstance(decision, dict)
            or decision.get("package_sha256") != row["package_sha256"]
            or not isinstance(decision.get("checks"), dict)
            or set(decision["checks"]) != REVIEW_CHECKS
            or any(type(value) is not bool for value in decision["checks"].values())):
            raise ValueError("code audit review differs from sealed finalist")
        if row["denied_egress_packets"] and decision["checks"]["cap_evasion"]:
            raise ValueError("code audit cannot clear denied worker egress")
        packages.append({
            "hotkey": row["hotkey"], "package_sha256": row["package_sha256"],
            "checks": {"build": row["build_passed"], **decision["checks"]},
        })
    return {
        "schema_version": AUDIT_SCHEMA,
        "round_id": evidence["round_id"],
        "evidence_sha256": canonical_sha256(evidence),
        "reviewer": review["reviewer"],
        "packages": packages,
    }


def verify_code_audit_receipt(
    receipt: Mapping[str, Any], evidence: Mapping[str, Any],
) -> dict[str, bool]:
    """Reject a stale or altered review before it can influence the crown."""
    if (receipt.get("schema_version") != AUDIT_SCHEMA
        or receipt.get("round_id") != evidence.get("round_id")
        or receipt.get("evidence_sha256") != canonical_sha256(evidence)
        or not isinstance(receipt.get("reviewer"), str) or not receipt["reviewer"]):
        raise ValueError("code audit receipt does not bind frozen evidence")
    receipts = receipt.get("packages")
    if not isinstance(receipts, list):
        raise ValueError("code audit receipt has no package decisions")
    decisions = {row["hotkey"]: row for row in receipts
                 if isinstance(row, dict) and isinstance(row.get("hotkey"), str)}
    expected = {row["hotkey"]: row for row in evidence["packages"]}
    if len(decisions) != len(receipts) or set(decisions) != set(expected):
        raise ValueError("code audit receipt omits a finalist")
    for hotkey, decision in decisions.items():
        row = expected[hotkey]
        checks = decision.get("checks")
        if (decision.get("package_sha256") != row["package_sha256"]
            or not isinstance(checks, dict)
            or set(checks) != REVIEW_CHECKS | {"build"}
            or any(type(value) is not bool for value in checks.values())
            or checks["build"] != row["build_passed"]
            or (row["denied_egress_packets"] > 0 and checks["cap_evasion"])):
            raise ValueError("code audit receipt differs from frozen package evidence")
    return {hotkey: all(decision["checks"].values()) for hotkey, decision in decisions.items()}
