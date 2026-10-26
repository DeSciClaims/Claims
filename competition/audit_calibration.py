"""Evaluate a proposed finalist code-audit policy on labeled testnet evidence.

The report measures mistakes; it does not silently select launch thresholds.
Labels must come from independently reviewed E0 controls and deliberate attacks.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from .code_audit import automatic_code_audit, validated_audit_policy
from .publication import canonical_sha256


CHECKS = frozenset({
    "build", "hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation",
})
SCHEMA = "claims_code_audit_calibration_v1"


def evaluate_audit_calibration(document: Mapping[str, Any]) -> dict[str, Any]:
    """Compare frozen audit decisions with manually labeled controls and attacks."""
    if (not isinstance(document, Mapping) or set(document) != {"schema_version", "policy", "cases"}
        or document["schema_version"] != SCHEMA):
        raise ValueError("audit calibration document is malformed")
    policy = validated_audit_policy(document["policy"])
    cases = document["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise ValueError("audit calibration needs 1 to 1000 labeled cases")
    seen: set[str] = set()
    counts: dict[str, dict[str, int]] = {
        check: {"expected_pass": 0, "expected_fail": 0,
                "false_reject": 0, "missed_attack": 0} for check in sorted(CHECKS)
    }
    # Lower values pass the three numeric checks. A policy can separate controls
    # from attacks only when max(control) is strictly below min(attack).
    measurements: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"expected_pass": [], "expected_fail": []},
    )
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"case_id", "evidence", "expected_checks"}:
            raise ValueError("audit calibration case is malformed")
        case_id = case["case_id"]
        evidence = case["evidence"]
        labels = case["expected_checks"]
        if (not isinstance(case_id, str) or not case_id or case_id in seen
            or not isinstance(evidence, dict) or not isinstance(labels, dict)):
            raise ValueError("audit calibration case ID, evidence or labels are invalid")
        seen.add(case_id)
        packages = evidence.get("packages")
        if (not isinstance(packages, list) or not packages
            or any(not isinstance(row, dict) or not isinstance(row.get("hotkey"), str)
                   for row in packages)):
            raise ValueError("audit calibration evidence has no finalists")
        hotkeys = [row["hotkey"] for row in packages]
        if len(set(hotkeys)) != len(hotkeys) or set(labels) != set(hotkeys):
            raise ValueError("audit calibration labels differ from finalists")
        actual = {row["hotkey"]: row["checks"]
                  for row in automatic_code_audit(evidence, policy)["packages"]}
        for row in packages:
            hotkey = row["hotkey"]
            expected = labels[hotkey]
            if (not isinstance(expected, dict) or set(expected) != CHECKS
                or any(type(value) is not bool for value in expected.values())):
                raise ValueError("audit calibration needs a Boolean label for every check")
            for check in CHECKS:
                should_pass = expected[check]
                did_pass = actual[hotkey][check]
                bucket = "expected_pass" if should_pass else "expected_fail"
                counts[check][bucket] += 1
                if should_pass and not did_pass:
                    counts[check]["false_reject"] += 1
                elif not should_pass and did_pass:
                    counts[check]["missed_attack"] += 1
            completed = row["completed_job_count"]
            total = row["final_job_count"]
            baseline = row["baseline_claim_count_median"]
            candidate = row["completed_claim_count_median"]
            values = {
                "hardcoded_output": row["max_same_statement_set_papers"] / completed
                if completed else None,
                "cap_evasion": row["cap_trip_count"] / total if total else None,
                "fragmentation": candidate / max(1, baseline)
                if isinstance(candidate, (int, float)) and isinstance(baseline, (int, float))
                else None,
            }
            for check, value in values.items():
                if value is not None:
                    bucket = "expected_pass" if expected[check] else "expected_fail"
                    measurements[check][bucket].append(value)
    envelopes = {}
    for check in ("hardcoded_output", "cap_evasion", "fragmentation"):
        control = measurements[check]["expected_pass"]
        attack = measurements[check]["expected_fail"]
        envelopes[check] = {
            "largest_expected_pass": max(control) if control else None,
            "smallest_expected_fail": min(attack) if attack else None,
            "separable_by_upper_threshold": bool(control and attack and max(control) < min(attack)),
        }
    return {
        "schema_version": SCHEMA,
        "input_sha256": canonical_sha256(document),
        "policy_sha256": canonical_sha256(policy),
        "case_count": len(cases),
        "package_count": sum(counts["build"][name] for name in ("expected_pass", "expected_fail")),
        "checks": counts,
        "numeric_envelopes": envelopes,
    }
