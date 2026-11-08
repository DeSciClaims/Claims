"""Execution concurrency comes from frozen operator rules, not a fixed ceiling."""

import pytest

from competition.execution_policy import (
    ExecutionPolicy,
    execution_policy,
    pipelined_execution,
)

FIELDS = ("bronze_max_workers", "scoring_max_workers", "decision_max_workers")


def _round(**values):
    return {"rules_public": {"policy": {"execution": {
        "schema_version": "claims_competition_execution_v1",
        **dict.fromkeys(FIELDS, 1), **values,
    }}}}


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [1, 32, 50, 64, 256, 1000])
def test_accepts_configured_positive_concurrency_without_an_artificial_ceiling(field, value):
    record = _round(**{field: value})
    limits = execution_policy(record)
    assert getattr(limits, field) == value
    assert pipelined_execution(record)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, 50.0, "50", None])
def test_rejects_nonpositive_or_noninteger_concurrency(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        execution_policy(_round(**{field: value}))


@pytest.mark.parametrize("record", [{}, {"rules_public": {}},
    {"rules_public": {"policy": {}}}, {"rules_public": {"policy": {"execution": None}}}])
def test_legacy_records_keep_single_worker_defaults(record):
    assert execution_policy(record) == ExecutionPolicy(1, 1, 1)


@pytest.mark.parametrize("change", ["missing", "extra", "schema", "not_object"])
def test_execution_contract_remains_strict(change):
    record = _round(scoring_max_workers=50)
    raw = record["rules_public"]["policy"]["execution"]
    if change == "missing":
        del raw["decision_max_workers"]
    elif change == "extra":
        raw["unknown_workers"] = 1
    elif change == "schema":
        raw["schema_version"] = "claims_competition_execution_v2"
    else:
        record["rules_public"]["policy"]["execution"] = []
    with pytest.raises(ValueError, match="positive integer"):
        execution_policy(record)
