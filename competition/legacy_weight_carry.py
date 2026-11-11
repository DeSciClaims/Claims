"""Signed, identity-bound carry of the final legacy extraction weight vector.

Capture and prove the historical UID-to-hotkey mapping before sealing. This
module does not query chain state or infer payee identities from current UIDs.
"""

from __future__ import annotations

import copy
from datetime import datetime
import json
import math
import re
from typing import Any, Mapping

from bittensor_wallet import Keypair

from .publication import canonical_sha256


LEGACY_WEIGHT_CARRY_SCHEMA = "claims_legacy_weight_carry_v1"
_BUNDLE_FIELDS = {
    "schema_version", "network", "netuid", "validator_hotkey", "snapshot_block",
    "snapshot_block_hash", "last_update_block", "last_update_at", "entries",
}
_RECEIPT_FIELDS = {"bundle", "bundle_sha256", "signer_hotkey", "signature_hex"}
_SUM_TOLERANCE = 1e-9


def _hotkey(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{40,64}", value):
        raise ValueError("legacy carry hotkey must be a valid SS58 address")
    try:
        key = Keypair(ss58_address=value)
        canonical = Keypair(public_key=key.public_key.hex(), ss58_format=42).ss58_address
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy carry hotkey must be a valid SS58 address") from exc
    if canonical != value:
        raise ValueError("legacy carry hotkey must use the Bittensor SS58 format")
    return value


def _public_provenance(value: Any, *, depth: int = 0) -> None:
    """Bound JSON evidence without accepting arbitrary objects or deep payloads."""
    if depth > 3:
        raise ValueError("legacy carry provenance is too deeply nested")
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str) and len(value) <= 4096:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict) and len(value) <= 32:
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ValueError("legacy carry provenance has an invalid key")
            _public_provenance(item, depth=depth + 1)
        return
    if isinstance(value, list) and len(value) <= 64:
        for item in value:
            _public_provenance(item, depth=depth + 1)
        return
    raise ValueError("legacy carry provenance must contain bounded public JSON evidence")


def _validated_bundle(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict)
        or set(value) - {"provenance"} != _BUNDLE_FIELDS
        or value.get("schema_version") != LEGACY_WEIGHT_CARRY_SCHEMA):
        raise ValueError("legacy carry bundle schema or fields are invalid")
    if (not isinstance(value["network"], str) or value["network"] not in {"mainnet", "testnet"}
        or type(value["netuid"]) is not int or value["netuid"] < 0):
        raise ValueError("legacy carry network or netuid is invalid")
    _hotkey(value["validator_hotkey"])
    if (any(type(value[key]) is not int or value[key] < 0
            for key in ("snapshot_block", "last_update_block"))
        or value["last_update_block"] > value["snapshot_block"]
        or not isinstance(value["snapshot_block_hash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["snapshot_block_hash"])):
        raise ValueError("legacy carry block evidence is invalid")
    try:
        timestamp = datetime.fromisoformat(value["last_update_at"])
        aware = timestamp.tzinfo is not None and timestamp.utcoffset() is not None
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy carry last-update timestamp must be timezone-aware ISO") from exc
    if not aware:
        raise ValueError("legacy carry last-update timestamp must be timezone-aware ISO")
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("legacy carry requires hotkey-bound weight entries")
    identities = set()
    weights = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"hotkey", "weight"}:
            raise ValueError("legacy carry entries must name hotkeys, not raw UIDs")
        hotkey = _hotkey(entry["hotkey"])
        weight = entry["weight"]
        if hotkey in identities or type(weight) not in {int, float}:
            raise ValueError("legacy carry entries repeat a hotkey or have invalid weights")
        try:
            valid_weight = math.isfinite(weight) and weight > 0
        except OverflowError:
            valid_weight = False
        if not valid_weight:
            raise ValueError("legacy carry entries have invalid weights")
        identities.add(hotkey)
        weights.append(weight)
    try:
        total = math.fsum(weights)
    except OverflowError as exc:
        raise ValueError("legacy carry weights must sum to one") from exc
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=_SUM_TOLERANCE):
        raise ValueError("legacy carry weights must sum to one")
    if "provenance" in value:
        if not isinstance(value["provenance"], dict):
            raise ValueError("legacy carry provenance must be a public evidence object")
        _public_provenance(value["provenance"])
        try:
            encoded = json.dumps(value["provenance"], ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("legacy carry provenance is not bounded JSON evidence") from exc
        if len(encoded.encode()) > 8192:
            raise ValueError("legacy carry provenance exceeds 8192 bytes")
    return copy.deepcopy(value)


def seal_legacy_weights(bundle: dict, signer: Any) -> dict[str, Any]:
    """Use the same digest-signing convention as signed competition receipts."""
    validated = _validated_bundle(bundle)
    if validated["validator_hotkey"] != getattr(signer, "ss58_address", None):
        raise ValueError("legacy carry validator differs from the signing hotkey")
    digest = canonical_sha256(validated)
    signature = signer.sign(bytes.fromhex(digest))
    return {"bundle": validated, "bundle_sha256": digest,
            "signer_hotkey": signer.ss58_address, "signature_hex": signature.hex()}


def verify_legacy_weights(
    receipt: Mapping[str, Any], *, expected_signer_hotkey: str, network: str, netuid: int,
) -> dict[str, Any]:
    """Fail closed on altered, unsigned, cross-network or UID-only carries."""
    if (not isinstance(network, str) or network not in {"mainnet", "testnet"}
        or type(netuid) is not int or netuid < 0):
        raise ValueError("expected legacy carry network or netuid is invalid")
    _hotkey(expected_signer_hotkey)
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("legacy carry receipt fields are invalid")
    bundle = _validated_bundle(receipt["bundle"])
    if bundle["network"] != network or bundle["netuid"] != netuid:
        raise ValueError("legacy carry belongs to another network or subnet")
    digest = canonical_sha256(bundle)
    if (bundle["validator_hotkey"] != expected_signer_hotkey
        or receipt["signer_hotkey"] != expected_signer_hotkey
        or receipt["bundle_sha256"] != digest):
        raise ValueError("legacy carry digest or signer differs")
    signature = receipt["signature_hex"]
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{128}", signature):
        raise ValueError("legacy carry signature is malformed")
    try:
        verified = Keypair(ss58_address=expected_signer_hotkey).verify(
            bytes.fromhex(digest), bytes.fromhex(signature),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy carry signature is malformed") from exc
    if not verified:
        raise ValueError("legacy carry signature is invalid")
    return bundle


def project_legacy_weights(
    bundle: dict, *, hotkeys_by_uid: list[str], owner_hotkey: str, burn_uid: int = 0,
    operator_hotkey: str | None = None, miner_only: bool = False,
    base_burn: float = 0.0,
) -> dict[int, float]:
    """Project signed historical shares; optionally remove and normalize the operator."""
    validated = _validated_bundle(bundle)
    if (not math.isfinite(base_burn) or not 0 <= base_burn < 1
        or (base_burn and not miner_only)):
        raise ValueError("base burn requires miner-only carry-forward")
    if (not isinstance(hotkeys_by_uid, list) or not hotkeys_by_uid
        or type(burn_uid) is not int or not 0 <= burn_uid < len(hotkeys_by_uid)
        or _hotkey(owner_hotkey) != hotkeys_by_uid[burn_uid]):
        raise ValueError("legacy carry burn UID is not the current subnet owner")
    hotkeys = [_hotkey(hotkey) for hotkey in hotkeys_by_uid]
    if len(set(hotkeys)) != len(hotkeys):
        raise ValueError("legacy carry metagraph contains duplicate hotkeys")
    uid_by_hotkey = {hotkey: uid for uid, hotkey in enumerate(hotkeys)}
    shares: dict[int, list[float]] = {}
    entries = validated["entries"]
    if miner_only:
        if not operator_hotkey:
            raise ValueError("miner-only legacy carry requires the operator hotkey")
        _hotkey(operator_hotkey)
        operator_rows = [entry for entry in entries if entry["hotkey"] == operator_hotkey]
        if len(operator_rows) != 1:
            raise ValueError("signed legacy carry must identify exactly one operator")
        entries = [entry for entry in entries if entry["hotkey"] != operator_hotkey]
        miner_total = math.fsum(entry["weight"] for entry in entries)
        if miner_total <= 0:
            raise ValueError("signed legacy carry has no miner allocation")
    else:
        miner_total = 1.0
    for entry in entries:
        uid = uid_by_hotkey.get(entry["hotkey"], burn_uid)
        shares.setdefault(uid, []).append(float(entry["weight"]) / miner_total * (1 - base_burn))
    if base_burn:
        shares.setdefault(burn_uid, []).append(base_burn)
    result = {uid: math.fsum(weights) for uid, weights in sorted(shares.items())}
    if not math.isclose(math.fsum(result.values()), 1.0, rel_tol=0, abs_tol=_SUM_TOLERANCE):
        raise ValueError("projected legacy carry weights do not sum to one")
    return result
