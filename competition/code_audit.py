"""Bind final code-review decisions to sealed packages and worker evidence.

Static code inspection cannot prove that a package lacks a hidden branch or
paper-keyed answer table. This module produces reproducible evidence and
requires explicit review decisions for those checks; it never infers a pass
from the absence of a simple string pattern.
"""

from __future__ import annotations

import ast
import hashlib
import math
from collections import Counter
from statistics import median
from typing import Any, Callable, Mapping

from miner.agent_v1.artifact_models import Artifact

from .duplicates import python_source_fingerprint
from .duplicate_candidates import VENDOR_PARTS, _archive_members, _without_docstrings
from .final_stage import effective_final_stage
from .publication import canonical_sha256


REVIEW_CHECKS = frozenset({"hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation"})
AUDIT_SCHEMA = "claims_final_code_audit_v1"
AUTO_REVIEWER = "automated:claims-code-audit-v1"
AUDIT_POLICY_FIELDS = frozenset({
    "min_completed_papers", "max_repeated_output_fraction",
    "max_cap_trip_fraction", "max_claim_count_ratio",
})
CONTEXT_MARKERS = ("input.json", "claims_competition_rounds_dir", "competition_round_id")


def validated_audit_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only calibrated, public thresholds; there are no hidden defaults."""
    if not isinstance(value, Mapping) or set(value) != AUDIT_POLICY_FIELDS:
        raise ValueError("code audit policy has missing or extra thresholds")
    count = value["min_completed_papers"]
    if type(count) is not int or not 2 <= count <= 10_000:
        raise ValueError("code audit minimum completed papers is invalid")
    parsed = {"min_completed_papers": count}
    for name in ("max_repeated_output_fraction", "max_cap_trip_fraction",
                 "max_claim_count_ratio"):
        raw = value[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"code audit {name} is invalid")
        try:
            number = float(raw)
        except OverflowError as exc:
            raise ValueError(f"code audit {name} is invalid") from exc
        if not math.isfinite(number):
            raise ValueError(f"code audit {name} is invalid")
        if ((name == "max_claim_count_ratio" and not 1 <= number <= 100)
            or (name != "max_claim_count_ratio" and not 0 <= number <= 1)):
            raise ValueError(f"code audit {name} is out of range")
        parsed[name] = number
    return parsed


def _source_indicators(package: bytes, paper_ids: list[str], pdf_hashes: list[str]) -> dict[str, Any]:
    """Read first-party Python literals without importing or executing a package."""
    paper_id_set = set(paper_ids)
    pdf_hash_set = set(pdf_hashes)
    matched_papers: set[str] = set()
    matched_hashes: set[str] = set()
    matched_context: set[str] = set()
    source_bytes = 0
    saw_source = False
    for name, body in _archive_members(package):
        parts = {part.lower() for part in name.replace("\\", "/").split("/")}
        if (not name.endswith(".py") or parts & VENDOR_PARTS
            or any(part.endswith(".dist-info") for part in parts)):
            continue
        saw_source = True
        source_bytes += len(body)
        if source_bytes > 20_000_000:
            raise ValueError("audit Python source exceeds scanner limit")
        try:
            tree = _without_docstrings(ast.parse(body.decode("utf-8-sig")))
        except (UnicodeError, SyntaxError, RecursionError) as exc:
            raise ValueError("audit Python source is invalid") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            literal = node.value
            if literal in paper_id_set:
                matched_papers.add(literal)
            if literal in pdf_hash_set:
                matched_hashes.add(literal)
            matched_context.update(marker for marker in CONTEXT_MARKERS if marker in literal.casefold())
    if not saw_source:
        raise ValueError("audit package has no Python source")
    return {
        "embedded_sample_paper_id_count": len(matched_papers),
        "embedded_sample_pdf_hash_count": len(matched_hashes),
        "evaluation_context_markers": sorted(matched_context),
    }


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
    pdf_hashes = [row["source_sha256"] for row in sample
                  if isinstance(row.get("source_sha256"), str)]
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
            indicators = _source_indicators(package, paper_ids, pdf_hashes)
            source_scan_error = None
        except ValueError as exc:
            # A malformed finalist archive must fail that finalist's build
            # check, not prevent every other finalist from being audited.
            fingerprint = None
            indicators = None
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
            "source_indicators": indicators,
            "source_scan_error": source_scan_error,
            "final_job_count": len(jobs_by_hotkey[hotkey]),
            "completed_job_count": statuses["completed"],
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


def automatic_code_audit(
    evidence: Mapping[str, Any], policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the published, calibrated thresholds to sealed final evidence."""
    thresholds = validated_audit_policy(policy)
    packages = []
    for row in evidence["packages"]:
        completed = row["completed_job_count"]
        total = row["final_job_count"]
        indicators = row["source_indicators"]
        if (type(completed) is not int or type(total) is not int or not 0 <= completed <= total
            or total < 1 or not isinstance(indicators, (dict, type(None)))):
            raise ValueError("automatic audit has malformed package evidence")
        sufficient = completed >= thresholds["min_completed_papers"]
        clean_source = bool(indicators is not None
                            and indicators["embedded_sample_paper_id_count"] == 0
                            and indicators["embedded_sample_pdf_hash_count"] == 0)
        baseline = row["baseline_claim_count_median"]
        candidate = row["completed_claim_count_median"]
        checks = {
            "build": row["build_passed"],
            "hardcoded_output": bool(
                sufficient and clean_source
                and row["max_same_statement_set_papers"] / completed
                <= thresholds["max_repeated_output_fraction"]
            ),
            "evaluation_context": bool(
                indicators is not None and not indicators["evaluation_context_markers"]
            ),
            "cap_evasion": bool(
                row["denied_egress_packets"] == 0
                and row["cap_trip_count"] / total <= thresholds["max_cap_trip_fraction"]
            ),
            "fragmentation": bool(
                sufficient and isinstance(candidate, (int, float))
                and isinstance(baseline, (int, float))
                and candidate <= max(1, baseline) * thresholds["max_claim_count_ratio"]
            ),
        }
        packages.append({
            "hotkey": row["hotkey"], "package_sha256": row["package_sha256"],
            "checks": checks,
        })
    return {
        "schema_version": AUDIT_SCHEMA,
        "round_id": evidence["round_id"],
        "evidence_sha256": canonical_sha256(evidence),
        "policy_sha256": canonical_sha256(thresholds),
        "reviewer": AUTO_REVIEWER,
        "packages": packages,
    }


def verify_code_audit_receipt(
    receipt: Mapping[str, Any], evidence: Mapping[str, Any],
    *, policy: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """Reject a stale or altered review before it can influence the crown."""
    if policy is not None:
        if dict(receipt) != automatic_code_audit(evidence, policy):
            raise ValueError("automatic code audit differs from frozen policy or evidence")
    elif receipt.get("reviewer") == AUTO_REVIEWER:
        raise ValueError("automatic code audit has no frozen policy")
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
