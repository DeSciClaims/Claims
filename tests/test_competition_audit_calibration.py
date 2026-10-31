"""Week-minus-one calibration reports both false rejections and missed attacks."""

from copy import deepcopy

import pytest

from competition.audit_calibration import CHECKS, SCHEMA, evaluate_audit_calibration


def _evidence():
    return {"schema_version": "claims_final_code_audit_v1", "round_id": "week-minus-one",
            "packages": [{
                "hotkey": "miner", "package_sha256": "a" * 64,
                "build_passed": True, "completed_job_count": 2, "final_job_count": 2,
                "source_indicators": {"evaluation_context_markers": []},
                "cap_trip_count": 0,
                "denied_egress_packets": 0,
                "completed_claim_count_median": 2, "baseline_claim_count_median": 2,
            }]}


def _policy():
    return {"min_completed_papers": 2, "max_cap_trip_fraction": 0.0, "max_claim_count_ratio": 2.0}


def _case(case_id, evidence, expected):
    return {"case_id": case_id, "evidence": evidence,
            "expected_checks": {"miner": {check: expected for check in CHECKS}}}


def test_calibration_reports_control_and_attack_outcomes_without_private_rows():
    clean = _evidence()
    attack = deepcopy(clean)
    row = attack["packages"][0]
    row["build_passed"] = False
    row["source_indicators"]["evaluation_context_markers"] = ["input.json"]
    row["cap_trip_count"] = 1
    row["completed_claim_count_median"] = 10
    document = {"schema_version": SCHEMA, "policy": _policy(),
                "cases": [_case("E0-control", clean, True),
                          _case("attack", attack, False)]}
    report = evaluate_audit_calibration(document)
    assert report["case_count"] == 2
    assert report["package_count"] == 2
    assert all(row == {"expected_pass": 1, "expected_fail": 1,
                       "false_reject": 0, "missed_attack": 0}
               for row in report["checks"].values())
    assert report["numeric_envelopes"]["fragmentation"] == {
        "largest_expected_pass": 1.0, "smallest_expected_fail": 5.0,
        "separable_by_upper_threshold": True,
    }
    assert "miner" not in str(report)
    assert evaluate_audit_calibration(document) == report


def test_calibration_exposes_threshold_errors_and_rejects_missing_labels():
    clean = _evidence()
    document = {"schema_version": SCHEMA,
                "policy": {**_policy(), "min_completed_papers": 3},
                "cases": [_case("E0-control", clean, True)]}
    report = evaluate_audit_calibration(document)
    assert report["checks"]["fragmentation"]["false_reject"] == 1
    assert report["numeric_envelopes"]["fragmentation"]["separable_by_upper_threshold"] is False
    document["cases"][0]["expected_checks"]["miner"].pop("fragmentation")
    with pytest.raises(ValueError, match="Boolean label"):
        evaluate_audit_calibration(document)
    document["cases"][0]["expected_checks"] = {"other": {check: True for check in CHECKS}}
    with pytest.raises(ValueError, match="labels differ"):
        evaluate_audit_calibration(document)


def test_calibration_counts_a_missed_attack():
    attack = _evidence()
    attack["packages"][0]["completed_claim_count_median"] = 10
    document = {"schema_version": SCHEMA,
                "policy": {**_policy(), "max_claim_count_ratio": 10.0},
                "cases": [_case("fragmentation-attack", attack, True)]}
    document["cases"][0]["expected_checks"]["miner"]["fragmentation"] = False
    report = evaluate_audit_calibration(document)
    assert report["checks"]["fragmentation"] == {
        "expected_pass": 0, "expected_fail": 1,
        "false_reject": 0, "missed_attack": 1,
    }
