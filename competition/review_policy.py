"""Consensus settings frozen in a competition's public rules.

This dependency-free module is mirrored in the backend. Historical rounds
without this object keep their original limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ControlledTestAccount:
    hotkey: str
    coldkey: str
    team_id: str


@dataclass(frozen=True)
class ReviewPolicy:
    reviewers_per_round: int = 20
    vote_quorum: int = 11
    agreement_percent: int = 75
    genuine_cases_per_round: int = 100
    synthetic_cases_per_bucket: int = 50
    response_deadline_seconds: int = 1800
    check_sample_size: int = 300
    check_min_decided: int = 60
    check_refusal_percent: int = 75
    escalation_limit: int = 1700
    controlled_test_accounts: tuple[ControlledTestAccount, ...] = field(
        default_factory=tuple
    )

    def controlled_account(
        self, hotkey: str, coldkey: str
    ) -> ControlledTestAccount | None:
        """An exception requires both public keys, never just a funding root."""
        return next(
            (
                row
                for row in self.controlled_test_accounts
                if row.hotkey == hotkey and row.coldkey == coldkey
            ),
            None,
        )

    def public(self) -> dict[str, Any]:
        from dataclasses import asdict

        body = asdict(self)
        body["controlled_test_accounts"] = [
            asdict(row) for row in self.controlled_test_accounts
        ]
        return {"schema_version": "claims_competition_consensus_v1", **body}


def review_policy(record: Mapping[str, Any]) -> ReviewPolicy:
    policy = (record.get("rules_public") or {}).get("policy") or {}
    if "test_participants" in policy:
        raise ValueError("funding exceptions are configured by the backend, not round rules")
    if policy.get("winner_selection") not in (None, "silver_tiebreak"):
        raise ValueError("unknown competition winner selection policy")
    if not uses_miner_review(record) and policy.get("review_execution") is not None:
        raise ValueError("Silver-only selection does not run competition review")
    if policy.get("team_identity") not in (None, "linked"):
        raise ValueError("unknown competition team identity policy")
    if policy.get("review_execution") not in (None, "controller"):
        raise ValueError("unknown competition review execution policy")
    bronze = policy.get("bronze")
    if bronze is not None and (
        not isinstance(bronze, dict)
        or set(bronze) not in ({"mode"}, {"mode", "lease_seconds"})
        or bronze["mode"] != "after_reveal"
    ):
        raise ValueError("unknown competition Bronze policy")
    if (
        isinstance(bronze, dict)
        and "lease_seconds" in bronze
        and (
            type(bronze["lease_seconds"]) is not int
            or not 120 <= bronze["lease_seconds"] <= 7200
        )
    ):
        raise ValueError("competition Bronze lease must be 120-7200 seconds")
    if policy.get("review_execution") == "controller" and not policy.get("consensus"):
        raise ValueError("controller review needs a frozen consensus policy")
    raw = policy.get("consensus")
    defaults = ReviewPolicy()
    if raw is None:
        return defaults
    if (
        record.get("network") not in {"mainnet", "testnet"}
        or not isinstance(raw, dict)
        or set(raw) != set(defaults.public())
        or raw.get("schema_version") != "claims_competition_consensus_v1"
    ):
        raise ValueError("competition consensus policy is malformed")
    bounds = {
        "reviewers_per_round": (1, 256),
        "vote_quorum": (1, 256),
        "agreement_percent": (51, 100),
        "genuine_cases_per_round": (1, 1000),
        "synthetic_cases_per_bucket": (1, 100),
        "response_deadline_seconds": (60, 1800),
        "check_sample_size": (1, 10000),
        "check_min_decided": (1, 10000),
        "check_refusal_percent": (51, 100),
        "escalation_limit": (0, 10000),
    }
    values = {}
    for key, (low, high) in bounds.items():
        value = raw[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"competition consensus {key} is invalid")
        values[key] = value
    if (
        values["vote_quorum"] > values["reviewers_per_round"]
        or values["check_min_decided"] > values["check_sample_size"]
    ):
        raise ValueError("competition consensus thresholds exceed their sample")
    accounts = raw["controlled_test_accounts"]
    if accounts != []:
        raise ValueError("controlled funding exceptions are configured by the backend, not round rules")
    controlled = []
    evaluation, schedule = policy.get("evaluation") or {}, policy.get("schedule") or {}
    smoke = (
        record.get("network") == "testnet"
        and isinstance(evaluation, dict)
        and evaluation.get("mode") == "testnet_smoke"
        and isinstance(schedule, dict)
        and schedule.get("mode") == "custom"
    )
    # Reduced thresholds and simulated operator identities are opt-in smoke
    # rules. The ordinary mainnet/testnet policy keeps the published rules.
    if not smoke and (
        controlled
        or any(
            values[key] != getattr(defaults, key)
            for key in (
                "vote_quorum",
                "agreement_percent",
                "response_deadline_seconds",
                "check_min_decided",
                "check_refusal_percent",
            )
        )
        or values["check_sample_size"] < defaults.check_sample_size
        or values["genuine_cases_per_round"] < defaults.genuine_cases_per_round
        or values["synthetic_cases_per_bucket"] < defaults.synthetic_cases_per_bucket
    ):
        raise ValueError(
            "reduced consensus rules and controlled accounts require a testnet smoke round"
        )
    return ReviewPolicy(**values, controlled_test_accounts=tuple(controlled))


def uses_miner_review(record: Mapping[str, Any]) -> bool:
    """Historical signed rounds retain their policy; new rounds use Silver only."""
    return ((record.get("rules_public") or {}).get("policy") or {}).get(
        "winner_selection"
    ) != "silver_tiebreak"
