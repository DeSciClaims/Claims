"""Capture and sign the final legacy allocation without submitting weights."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from competition.legacy_weight_carry import (
    LEGACY_WEIGHT_CARRY_SCHEMA,
    seal_legacy_weights,
    verify_legacy_weights,
)
from competition.publication import canonical_sha256

_PENDING_GROUP_LIMIT = 257


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _hash(value: Any) -> str:
    result = str(value).removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ValueError("chain returned an invalid block hash")
    return result


def _timestamp(value: Any) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("last-update evidence needs a timezone-aware timestamp")
    return result.astimezone(UTC)


def _pending_commits(substrate: Any, *, netuid: int, block_hash: str, signer: str) -> int:
    """Inspect every bounded CRv4 storage group, never retaining ciphertext."""
    groups = 0
    for _, commits in substrate.query_map(
        module="SubtensorModule", storage_function="TimelockedWeightCommits",
        params=[netuid], block_hash=block_hash,
        page_size=64, max_results=_PENDING_GROUP_LIMIT,
    ):
        groups += 1
        if groups >= _PENDING_GROUP_LIMIT:
            raise ValueError("pending weight commitment query exceeded its safe bound")
        for commit in _value(commits):
            commit = _value(commit)
            if not isinstance(commit, (tuple, list)) or len(commit) != 4:
                raise ValueError("pending weight commitment has an unknown format")
            account = _value(commit[0])
            if not isinstance(account, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{40,64}", account):
                raise ValueError("pending weight commitment has an unknown account format")
            if account == signer:
                raise ValueError("validator has pending timelocked weight commits; wait for reveal")
    return groups


def _corroborate(evidence: dict, *, network: str, netuid: int, signer: str,
                 rows: list[dict]) -> dict:
    event, run = evidence.get("event"), evidence.get("run")
    if (evidence.get("schema_version") != "claims_legacy_weight_evidence_v1"
        or evidence.get("network") != network or evidence.get("netuid") != netuid
        or not isinstance(event, dict) or not isinstance(run, dict)
        or event.get("network") != network or event.get("status") != "success"
        or run.get("network") != network or run.get("status") != "completed"
        or run.get("validator_hotkey") != signer or event.get("run_id") != run.get("run_id")
        or not isinstance(event.get("event_id"), str) or not event["event_id"]
        or not isinstance(run.get("run_id"), str) or not run["run_id"]):
        raise ValueError("backend success evidence does not match this validator and network")
    weights = {}
    for item in event.get("weights", []):
        uid, weight = item.get("uid"), item.get("weight")
        if (type(uid) is not int or uid < 0 or uid in weights
            or type(weight) not in {int, float} or not math.isfinite(weight) or weight < 0):
            raise ValueError("backend success event contains invalid weights")
        weights[uid] = weight
    if not weights or not math.isclose(math.fsum(weights.values()), 1, abs_tol=1e-8):
        raise ValueError("backend success weights do not sum to one")
    maximum = max(weights.values())
    # The SDK max-upscales the submitted floats before rounding to uint16.
    emitted = {uid: round(weight / maximum * 65535) for uid, weight in weights.items()
               if round(weight / maximum * 65535) > 0}
    if emitted != {row["uid"]: row["weight_u16"] for row in rows}:
        raise ValueError("backend success event differs from the finalized on-chain allocation")
    miners = {}
    for miner in run.get("target_miners", []):
        uid = miner.get("uid")
        if type(uid) is not int or uid in miners:
            raise ValueError("backend run contains invalid miner identities")
        miners[uid] = miner
    for row in rows:
        if row["uid"] == event.get("operator_uid"):
            continue
        miner = miners.get(row["uid"], {})
        if miner.get("hotkey") != row["hotkey"] or miner.get("coldkey") != row["coldkey"]:
            raise ValueError("backend run identity differs from the captured payee")
    return event


def capture_bundle(subtensor: Any, *, network: str, netuid: int, validator_hotkey: str,
                   backend_evidence: dict, last_update_at: str | None = None,
                   timestamp_source_reason: str | None = None) -> dict:
    """Read one finalized state and prove the original receiving identities."""
    substrate = subtensor.substrate
    head = substrate.get_chain_finalised_head()
    snapshot = int(substrate.get_block_number(head))
    info = subtensor.get_metagraph_info(netuid=netuid, block=snapshot)
    hotkeys, coldkeys = list(info.hotkeys), list(info.coldkeys)
    registered = list(info.block_at_registration)
    if (len(set(hotkeys)) != len(hotkeys) or len(coldkeys) != len(hotkeys)
        or len(registered) != len(hotkeys) or hotkeys.count(validator_hotkey) != 1):
        raise ValueError("finalized metagraph does not identify this validator safely")
    uid = hotkeys.index(validator_hotkey)
    neuron = subtensor.neuron_for_uid(netuid=netuid, uid=uid, block=snapshot)
    if neuron.hotkey != validator_hotkey:
        raise ValueError("finalized weight row belongs to another validator")
    updated = int(neuron.last_update)
    if not 0 < updated <= snapshot:
        raise ValueError("validator has no valid last weight update")
    raw = _value(substrate.query(module="SubtensorModule", storage_function="Weights",
                                params=[netuid, uid], block_hash=head))
    rows, seen = [], set()
    for destination, weight in raw:
        if (type(destination) is not int or destination in seen or not 0 <= destination < len(hotkeys)
            or type(weight) is not int or not 0 < weight <= 65535):
            raise ValueError("finalized allocation contains invalid raw weights")
        seen.add(destination)
        registration = int(registered[destination])
        if not 0 <= registration <= updated:
            raise ValueError("payee UID was reassigned after the last update; historical identity proof is required")
        rows.append({"uid": destination, "weight_u16": weight, "hotkey": hotkeys[destination],
                     "coldkey": coldkeys[destination], "registration_block": registration})
    rows.sort(key=lambda item: item["uid"])
    if not rows:
        raise ValueError("finalized allocation is empty")
    groups = _pending_commits(substrate, netuid=netuid, block_hash=head, signer=validator_hotkey)
    event = _corroborate(backend_evidence, network=network, netuid=netuid,
                         signer=validator_hotkey, rows=rows)
    source = "chain_timestamp"
    try:
        timestamp = _timestamp(subtensor.get_timestamp(block=updated))
    except Exception as exc:
        pruned = any(marker in str(exc).lower() for marker in ("state already discarded", "unknownblock", "pruned"))
        if (not pruned or last_update_at is None or not timestamp_source_reason
            or not timestamp_source_reason.strip() or len(timestamp_source_reason) > 512):
            raise ValueError("historical timestamp unavailable; provide a corroborated --last-update-at and --timestamp-source-reason") from exc
        timestamp = _timestamp(last_update_at)
        if timestamp != _timestamp(event["created_at"]):
            raise ValueError("timestamp override must equal the backend success event time") from exc
        source = "backend_success_event"
    else:
        if last_update_at is not None and _timestamp(last_update_at) != timestamp:
            raise ValueError("timestamp override conflicts with the chain timestamp")
    captured_at = _timestamp(subtensor.get_timestamp(block=snapshot))
    if timestamp > captured_at:
        raise ValueError("last-update timestamp is after the finalized snapshot")
    total = sum(row["weight_u16"] for row in rows)
    provenance = {
        "source": "finalized_chain_allocation", "validator_uid": uid,
        "snapshot_at": captured_at.isoformat(), "raw_entries": rows,
        "last_update_block_hash": _hash(substrate.get_block_hash(updated)),
        "identity_proof": "all_payees_registered_before_last_update",
        "pending_timelocked_commits": [], "pending_storage_groups_checked": groups,
        "backend_event_id": event["event_id"], "backend_run_id": event["run_id"],
        "backend_evidence_sha256": canonical_sha256(backend_evidence),
        "timestamp_source": source,
    }
    if source == "backend_success_event":
        provenance["timestamp_source_reason"] = timestamp_source_reason.strip()
    return {
        "schema_version": LEGACY_WEIGHT_CARRY_SCHEMA, "network": network, "netuid": netuid,
        "validator_hotkey": validator_hotkey, "snapshot_block": snapshot,
        "snapshot_block_hash": _hash(head), "last_update_block": updated,
        "last_update_at": timestamp.isoformat(),
        "entries": [{"hotkey": row["hotkey"], "weight": row["weight_u16"] / total} for row in rows],
        "provenance": provenance,
    }


def write_receipt(path: Path, receipt: dict) -> None:
    """Public signatures contain no secrets; never replace an existing capture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        raise ValueError("capture output already exists; choose a fresh path")
    descriptor, temporary = tempfile.mkstemp(prefix=".legacy-weight-capture-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(receipt, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        # Link is atomic and refuses overwrite, including a concurrent writer.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("mainnet", "testnet"), required=True)
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--subtensor-network", choices=("finney", "test"), required=True)
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", required=True)
    parser.add_argument("--wallet-path", type=Path)
    parser.add_argument("--backend-evidence", type=Path, required=True,
                        help="Verified backend success event and original run identity export")
    parser.add_argument("--last-update-at", help="Explicit acknowledgement time if historical chain state is pruned")
    parser.add_argument("--timestamp-source-reason")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.netuid < 0 or args.subtensor_network != {"mainnet": "finney", "testnet": "test"}[args.network]:
        parser.error("network/subtensor-network mismatch or invalid netuid")
    if os.path.lexists(args.output):
        parser.error("capture output already exists")
    if args.backend_evidence.stat().st_size > 100_000:
        parser.error("backend evidence export exceeds 100KB")
    evidence = json.loads(args.backend_evidence.read_text())
    # No coldkey access, transaction construction, or weight submission occurs.
    import bittensor as bt

    kwargs = {"name": args.wallet_name, "hotkey": args.wallet_hotkey}
    if args.wallet_path is not None:
        kwargs["path"] = str(args.wallet_path)
    wallet = bt.Wallet(**kwargs)
    chain = bt.Subtensor(network=args.subtensor_network)
    try:
        bundle = capture_bundle(chain, network=args.network, netuid=args.netuid,
            validator_hotkey=wallet.hotkey.ss58_address, backend_evidence=evidence,
            last_update_at=args.last_update_at, timestamp_source_reason=args.timestamp_source_reason)
        receipt = seal_legacy_weights(bundle, wallet.hotkey)
        verify_legacy_weights(receipt, expected_signer_hotkey=wallet.hotkey.ss58_address,
                              network=args.network, netuid=args.netuid)
        write_receipt(args.output, receipt)
    finally:
        close = getattr(chain, "close", None)
        if close:
            close()
    print(json.dumps({"status": "captured", "bundle_sha256": receipt["bundle_sha256"],
                      "snapshot_block": bundle["snapshot_block"], "entries": len(bundle["entries"]),
                      "timestamp_source": bundle["provenance"]["timestamp_source"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
