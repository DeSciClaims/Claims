"""Canonical signed public receipt for a completed competition round."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


PUBLICATION_SCHEMA = "claims_competition_publication_v1"
FROZEN_FIELDS = (
    "round_id", "network", "archive_block_hash", "drand_round",
    "drand_seed", "paper_pool_sha256", "rules_sha256", "configuration_sha256",
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def publication_bundle(
    round_record: Mapping[str, Any], *, results: Mapping[str, Any],
    weights: Mapping[str, Any],
) -> dict[str, Any]:
    if any(round_record.get(field) is None for field in FROZEN_FIELDS):
        raise ValueError("round is missing frozen publication fields")
    if not results or not weights:
        raise ValueError("publication requires results and weights")
    return {
        "schema_version": PUBLICATION_SCHEMA,
        **{field: round_record[field] for field in FROZEN_FIELDS},
        "results": dict(results), "weights": dict(weights),
    }


def sign_publication(bundle: Mapping[str, Any], hotkey: Any) -> dict[str, Any]:
    if bundle.get("schema_version") != PUBLICATION_SCHEMA:
        raise ValueError("invalid publication bundle schema")
    digest = canonical_sha256(bundle)
    signature = hotkey.sign(bytes.fromhex(digest))
    return {
        "bundle": dict(bundle), "bundle_sha256": digest,
        "signer_hotkey": hotkey.ss58_address,
        "signature_hex": signature.hex(),
    }
