"""Explicit, public workload reduction for a testnet integration rehearsal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .round_schedule import schedule_for_round


SMOKE_PROFILE_ID = "testnet_smoke_v1"


@dataclass(frozen=True)
class SmokeProfile:
    paper_count: int = 2
    known_answer_count: int = 1


def smoke_profile(record: Mapping[str, Any]) -> SmokeProfile | None:
    policy = ((record.get("rules_public") or {}).get("policy") or {})
    value = policy.get("test_profile")
    if value is None:
        return None
    if (value != SMOKE_PROFILE_ID or record.get("network") != "testnet"
        or record.get("incumbent_hotkey") is not None
        or not schedule_for_round(record).custom):
        raise ValueError("test profile requires a week-one testnet custom round")
    return SmokeProfile()
