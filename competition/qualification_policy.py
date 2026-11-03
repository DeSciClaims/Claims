"""Validate immutable entry-policy evidence supplied by the backend."""
from typing import Any


def required_consensus_rounds(policy: dict[str, Any] | None, network: str | None) -> int:
    if policy is None:
        return 2
    if not isinstance(policy, dict):
        raise ValueError("invalid backend qualification policy")
    required = policy.get("required_consensus_rounds")
    if (type(required) is not int or required not in {0, 2}
        or network not in {"mainnet", "testnet"}
        or (network == "mainnet" and required != 2)
        or policy != {
            "schema_version": "claims_competition_qualification_v1",
            "source": "backend", "network": network,
            "required_consensus_rounds": required, "window_days": 7,
        }):
        raise ValueError("invalid backend qualification policy")
    return required
