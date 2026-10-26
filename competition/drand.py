"""Select a post-deadline drand beacon using the verifying drand-client CLI.

The official CLI verifies the BLS beacon. We additionally pin the chain
identity and recompute randomness from the returned signature. A missing CLI
or mismatched identity fails closed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_CHAIN_HASH = "8990e7a9aaed2ffed73dbd7092123d6f289930540d7651336225dc172e51b2ce"
DEFAULT_PUBLIC_KEY = "868f005eb8e6e4ca0a47c8a77ceaa5309a47978a7c71bc5cce96366b5d7a569937c529eeda66c7293784a9402801af31"


@dataclass(frozen=True)
class VerifiedBeacon:
    round: int
    seed: bytes
    signature_hex: str
    chain_hash: str
    public_key: str
    scheduled_at: int


def first_round_after(timestamp: int, *, genesis_time: int, period_seconds: int) -> int:
    if timestamp < 0 or genesis_time < 0 or period_seconds < 1:
        raise ValueError("invalid drand timing")
    return 1 if timestamp < genesis_time else (timestamp - genesis_time) // period_seconds + 2


def _run_cli(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        response = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError("verified drand-client request failed") from exc
    if not isinstance(response, dict):
        raise RuntimeError("drand-client returned a non-object response")
    return response


def fetch_verified_beacon(
    *, block_timestamp: int, chain_hash: str = DEFAULT_CHAIN_HASH,
    public_key: str = DEFAULT_PUBLIC_KEY,
    relay_url: str = "https://api.drand.sh",
    executable: str = "drand-cli",
    run_cli: Callable[[list[str]], dict[str, Any]] = _run_cli,
) -> VerifiedBeacon:
    """Fetch the first BLS-verified beacon strictly after block D+600."""
    if not relay_url.startswith("https://") or not chain_hash or not public_key:
        raise ValueError("drand chain identity and HTTPS relay are required")
    url = relay_url.rstrip("/") + "/" + chain_hash
    prefix = [executable, "get"]
    info = run_cli(prefix + ["chain-info", "--url", url, "--insecure"])
    if str(info.get("hash") or "").lower() != chain_hash or str(info.get("public_key") or "").lower() != public_key:
        raise RuntimeError("drand relay chain identity differs from pinned rules")
    try:
        genesis = int(info["genesis_time"])
        period = int(info["period"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("drand chain timing is malformed") from exc
    target_round = first_round_after(block_timestamp, genesis_time=genesis, period_seconds=period)
    response = run_cli(prefix + ["public", "--url", url, "--insecure", str(target_round)])
    try:
        signature_hex = str(response["signature"]).lower()
        signature = bytes.fromhex(signature_hex)
        returned_round = int(response["round"])
        advertised = str(response.get("randomness") or "").lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("drand beacon is malformed") from exc
    if returned_round != target_round or len(signature) < 48:
        raise RuntimeError("drand beacon has the wrong round or signature")
    seed = hashlib.sha256(signature).digest()
    if advertised and advertised != seed.hex():
        raise RuntimeError("drand randomness differs from verified signature")
    return VerifiedBeacon(
        target_round, seed, signature_hex, chain_hash, public_key,
        genesis + (target_round - 1) * period,
    )
