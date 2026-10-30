"""Concurrency limits frozen before intake, independent of host credentials."""

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ExecutionPolicy:
    bronze_max_workers: int = 1
    scoring_max_workers: int = 1
    decision_max_workers: int = 1


def execution_policy(record: Mapping[str, Any]) -> ExecutionPolicy:
    raw = (record.get("rules_public", {}).get("policy", {}) or {}).get("execution")
    if raw is None:
        return ExecutionPolicy()
    fields = {"bronze_max_workers", "scoring_max_workers", "decision_max_workers"}
    if (not isinstance(raw, dict)
        or set(raw) != fields | {"schema_version"}
        or raw["schema_version"] != "claims_competition_execution_v1"
        or any(type(raw[key]) is not int or not 1 <= raw[key] <= 32 for key in fields)):
        raise ValueError("competition execution needs integer concurrency limits in [1,32]")
    return ExecutionPolicy(**{key: raw[key] for key in fields})


def pipelined_execution(record: Mapping[str, Any]) -> bool:
    execution_policy(record)
    return "execution" in (record.get("rules_public", {}).get("policy", {}) or {})
