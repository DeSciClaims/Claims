import pytest

from competition.failure_policy import IncumbentFailureSummary
from competition.jobs import build_jobs
from competition.reference_rerun import reference_rerun_plan


def _inputs():
    return {
        "failure": IncumbentFailureSummary("screen", 2, 1),
        "round_record": {"incumbent_hotkey": "king", "baseline_package_sha256": "a" * 64,
                         "reference_package_sha256": "b" * 64},
        "source_receipt": {"stage": "screen", "entries": [
            {"hotkey": "king", "package_sha256": "a" * 64,
             "team_id": "old-team", "role": "incumbent", "position": 0},
            {"hotkey": "miner", "package_sha256": "c" * 64,
             "team_id": "new-team", "role": "challenger", "position": 1},
        ]},
        "sample": [{"stage": "screen", "paper_id": "p2", "position": 1,
                    "judge_id": "J2", "source_sha256": "2" * 64},
                   {"stage": "screen", "paper_id": "p1", "position": 0,
                    "judge_id": "J1", "source_sha256": "1" * 64},
                   {"stage": "final", "paper_id": "p3", "position": 0,
                    "judge_id": "J1", "source_sha256": "3" * 64}],
        "reference": {"hotkey": "team-reference", "package_sha256": "b" * 64,
                      "team_id": "claims-reference"},
    }


def test_reference_rerun_uses_same_papers_judges_and_challenger_packages():
    plan = reference_rerun_plan(**_inputs())
    assert plan.source_stage == "screen"
    assert [(row["paper_id"], row["judge_id"], row["source_sha256"], row["stage"])
            for row in plan.papers] == [
        ("p1", "J1", "1" * 64, "rerun"),
        ("p2", "J2", "2" * 64, "rerun"),
    ]
    assert [(row["role"], row["hotkey"], row["package_sha256"]) for row in plan.entries] == [
        ("reference", "team-reference", "b" * 64),
        ("challenger", "miner", "c" * 64),
    ]
    jobs = build_jobs(round_id="week-2", selected=list(plan.entries),
                      sample=list(plan.papers), stage="rerun",
                      configuration_sha256="d" * 64)
    assert {(job["paper_id"], job["hotkey"]) for job in jobs} == {
        ("p1", "team-reference"), ("p1", "miner"),
        ("p2", "team-reference"), ("p2", "miner"),
    }


def test_reference_rerun_rejects_threshold_and_reference_substitution():
    values = _inputs()
    values["failure"] = IncumbentFailureSummary("screen", 5, 1)
    with pytest.raises(ValueError, match="eligible failed incumbent"):
        reference_rerun_plan(**values)
    values = _inputs()
    values["reference"] = {**values["reference"], "package_sha256": "d" * 64}
    with pytest.raises(ValueError, match="differs from frozen round"):
        reference_rerun_plan(**values)
    values = _inputs()
    values["sample"][0] = {**values["sample"][0], "paper_id": "p1"}
    with pytest.raises(ValueError, match="distinct paper"):
        reference_rerun_plan(**values)
