"""Verify a weekly publication and project its roles to current chain UIDs."""

from __future__ import annotations

import math
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from bittensor_wallet import Keypair

from .publication import FROZEN_FIELDS, PUBLICATION_SCHEMA, canonical_sha256

ROLE_SHARES = {
    "operator": 0.90,
    "production": 0.07,
    "runner_up_1": 0.02,
    "runner_up_2": 0.01,
}


def verify_weekly_receipt(
    receipt: Mapping[str, Any], *, expected_signer_hotkey: str, network: str,
) -> dict[str, Any]:
    """Reject altered, unsigned, cross-network, or malformed publications."""
    if network not in {"testnet", "mainnet"} or not expected_signer_hotkey:
        raise ValueError("expected signer and competition network are required")
    bundle = receipt.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("schema_version") != PUBLICATION_SCHEMA:
        raise ValueError("invalid weekly publication schema")
    if (bundle.get("network") != network
        or any(bundle.get(field) is None for field in FROZEN_FIELDS)
        or not isinstance(bundle.get("results"), dict)
        or not bundle["results"].get("decision")):
        raise ValueError("weekly publication is for another network or no round")
    digest = canonical_sha256(bundle)
    if digest != receipt.get("bundle_sha256") or receipt.get("signer_hotkey") != expected_signer_hotkey:
        raise ValueError("weekly publication digest or signer differs")
    try:
        verified = Keypair(ss58_address=expected_signer_hotkey).verify(
            bytes.fromhex(digest), bytes.fromhex(str(receipt["signature_hex"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("weekly publication signature is malformed") from exc
    if not verified:
        raise ValueError("weekly publication signature is invalid")
    weights = bundle.get("weights")
    if not isinstance(weights, dict) or not isinstance(weights.get("vector"), list):
        raise ValueError("weekly publication has no weight vector")
    vector = weights["vector"]
    try:
        vector_rows = [(row["uid"], row["weight"]) for row in vector]
    except (KeyError, TypeError) as exc:
        raise ValueError("weekly publication weight vector is malformed") from exc
    if (not vector_rows or len({uid for uid, _ in vector_rows}) != len(vector_rows)
        or any(isinstance(uid, bool) or not isinstance(uid, int) or uid < 0
               or isinstance(weight, bool) or not isinstance(weight, int | float)
               or not math.isfinite(weight) or weight <= 0
               for uid, weight in vector_rows)
        or not math.isclose(sum(weight for _, weight in vector_rows), 1.0, abs_tol=1e-9)
        or sum(math.isclose(weight, 0.90, abs_tol=1e-9)
               for _, weight in vector_rows) != 1):
        raise ValueError("weekly publication weight vector is invalid")
    _validated_roles(weights.get("roles"))
    return bundle


def _validated_roles(value: Any) -> dict[str, tuple[str, bool]]:
    if not isinstance(value, list) or len(value) != len(ROLE_SHARES):
        raise ValueError("weekly publication must name four payout roles")
    roles: dict[str, tuple[str, bool]] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("weekly payout role is malformed")
        raw_role, hotkey, share = row.get("role"), row.get("hotkey"), row.get("share")
        if not isinstance(raw_role, str) or not isinstance(hotkey, str):
            raise ValueError("weekly payout role identity is malformed")
        burned = raw_role.endswith("_burned")
        role = raw_role.removesuffix("_burned")
        if (role not in ROLE_SHARES or role in roles
            or isinstance(share, bool) or not isinstance(share, int | float)
            or not math.isclose(share, ROLE_SHARES[role], abs_tol=1e-12)
            or (role == "operator" and (burned or not hotkey))
            or (not burned and not hotkey)):
            raise ValueError("weekly payout role or share is invalid")
        roles[role] = (hotkey, burned)
    if set(roles) != set(ROLE_SHARES):
        raise ValueError("weekly payout roles are incomplete")
    active_hotkeys = [hotkey for hotkey, burned in roles.values() if not burned]
    if len(active_hotkeys) != len(set(active_hotkeys)):
        raise ValueError("weekly payout roles repeat an active hotkey")
    return roles


def project_weekly_weights(
    bundle: Mapping[str, Any], *, hotkeys_by_uid: list[str],
    owner_hotkey: str, operator_hotkey: str, burn_uid: int = 0,
) -> dict[int, float]:
    """Re-resolve signed role shares; burn absent or explicitly vacant slots."""
    if (not hotkeys_by_uid or not 0 <= burn_uid < len(hotkeys_by_uid)
        or hotkeys_by_uid[burn_uid] != owner_hotkey):
        raise ValueError("configured burn UID is not the current subnet owner")
    if len(set(hotkeys_by_uid)) != len(hotkeys_by_uid):
        raise ValueError("metagraph contains duplicate hotkeys")
    weights = bundle.get("weights")
    if not isinstance(weights, Mapping):
        raise ValueError("weekly publication has no weights")
    roles = _validated_roles(weights.get("roles"))
    if roles["operator"] != (operator_hotkey, False):
        raise ValueError("weekly operator differs from configured operator")
    uid_by_hotkey = {hotkey: uid for uid, hotkey in enumerate(hotkeys_by_uid)}
    operator_uid = uid_by_hotkey.get(operator_hotkey)
    if operator_uid is None or operator_uid == burn_uid:
        raise ValueError("weekly operator is not registered as a receiving UID")
    result: dict[int, float] = {}
    for role, share in ROLE_SHARES.items():
        hotkey, burned = roles[role]
        uid = burn_uid if burned else uid_by_hotkey.get(hotkey, burn_uid)
        if uid == operator_uid and role != "operator":
            raise ValueError("miner payout resolves to operator UID")
        if uid == burn_uid and not burned and hotkey == owner_hotkey:
            raise ValueError("miner payout names the owner hotkey")
        result[uid] = result.get(uid, 0.0) + share
    if not math.isclose(sum(result.values()), 1.0, abs_tol=1e-12):
        raise AssertionError("projected weekly weight vector does not sum to one")
    return dict(sorted(result.items()))


def fetch_latest_publication(*, backend_url: str, network: str) -> dict[str, Any] | None:
    """Read the newest published receipt; a current unpublished week is ignored."""
    parsed = urllib.parse.urlparse(backend_url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("competition backend must use HTTPS or loopback")
    base = backend_url.rstrip("/")
    with urllib.request.urlopen(
        base + "/public/competition/rounds?network=" + network + "&limit=100", timeout=15,
    ) as response:
        rounds = json.load(response)
    if not isinstance(rounds, list):
        raise ValueError("competition round listing is malformed")
    published = next((row for row in rounds if row.get("status") == "published"), None)
    if published is None:
        return None
    round_id = str(published["round_id"])
    with urllib.request.urlopen(
        base + "/public/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/results",
        timeout=15,
    ) as response:
        receipt = json.load(response)
    if not isinstance(receipt, dict):
        raise ValueError("competition publication is malformed")
    return receipt


class WeeklyWeightHeartbeat:
    """Keep one verified receipt across backend outages and submit each tempo."""

    def __init__(
        self, *, network: str, netuid: int, backend_url: str,
        operator_hotkey: str, wallet: Any, subtensor: Any,
        cache_path: Path, weight_period: int = 16,
        fetcher: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        if not operator_hotkey or netuid < 0 or weight_period < 1:
            raise ValueError("invalid weekly weight heartbeat configuration")
        self.network = network
        self.netuid = netuid
        self.operator_hotkey = operator_hotkey
        self.wallet = wallet
        self.subtensor = subtensor
        self.cache_path = cache_path
        self.weight_period = weight_period
        self.expected_signer = wallet.hotkey.ss58_address
        self.fetcher = fetcher or (lambda: fetch_latest_publication(
            backend_url=backend_url, network=network,
        ))
        self.last_submitted_block = 0

    def _load_cache(self) -> dict[str, Any] | None:
        try:
            receipt = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        verify_weekly_receipt(
            receipt, expected_signer_hotkey=self.expected_signer, network=self.network,
        )
        return receipt

    def _save_cache(self, receipt: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(self.cache_path.name + ".tmp")
        temporary.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.cache_path)

    def _current_receipt(self) -> tuple[dict[str, Any] | None, str]:
        cached = self._load_cache()
        try:
            fetched = self.fetcher()
            if fetched is None:
                return cached, "cached" if cached else "unpublished"
            new_bundle = verify_weekly_receipt(
                fetched, expected_signer_hotkey=self.expected_signer, network=self.network,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if cached is None:
                raise RuntimeError("no valid weekly receipt is available") from exc
            return cached, "cached_after_fetch_error"
        if cached is not None:
            old_bundle = cached["bundle"]
            new_time, old_time = int(new_bundle["drand_scheduled_at"]), int(old_bundle["drand_scheduled_at"])
            if new_time < old_time or (new_time == old_time and fetched["bundle_sha256"] != cached["bundle_sha256"]):
                return cached, "cached_after_rollback"
            if fetched["bundle_sha256"] == cached["bundle_sha256"]:
                return cached, "cached_current"
        self._save_cache(fetched)
        return fetched, "fresh"

    def step(self, *, audit_only: bool = False) -> dict[str, Any]:
        receipt, source = self._current_receipt()
        if receipt is None:
            return {"status": "waiting_for_publication", "source": source}
        bundle = receipt["bundle"]
        info = self.subtensor.get_metagraph_info(netuid=self.netuid)
        if info is None:
            raise RuntimeError("subnet metagraph is unavailable")
        hotkeys = list(info.hotkeys)
        if self.expected_signer not in hotkeys:
            raise ValueError("validator signer is not registered on the subnet")
        uid = hotkeys.index(self.expected_signer)
        permits = list(info.validator_permit)
        if len(permits) != len(hotkeys) or not permits[uid]:
            raise ValueError("weekly weight signer lacks a validator permit")
        last_updates = list(info.last_update)
        if len(last_updates) != len(hotkeys):
            raise ValueError("metagraph last-update data is incomplete")
        block = int(self.subtensor.get_current_block())
        last_update = max(int(last_updates[uid]), self.last_submitted_block)
        if block < last_update:
            raise ValueError("chain block precedes validator's last weight update")
        tempo = int(info.tempo)
        if tempo < 1:
            raise ValueError("subnet tempo is invalid")
        due_after = min(tempo, 1800)
        weights_by_uid = project_weekly_weights(
            bundle, hotkeys_by_uid=hotkeys, owner_hotkey=str(info.owner_hotkey),
            operator_hotkey=self.operator_hotkey,
        )
        if audit_only:
            return {"status": "audit_only", "round_id": bundle["round_id"],
                    "source": source, "current_block": block,
                    "due_block": last_update + due_after, "weights": weights_by_uid}
        if block - last_update < due_after:
            return {"status": "waiting_for_tempo", "round_id": bundle["round_id"],
                    "source": source, "current_block": block, "due_block": last_update + due_after,
                    "alert_stale": block - int(last_updates[uid]) >= 2000}
        response = self.subtensor.set_weights(
            wallet=self.wallet, netuid=self.netuid,
            uids=list(weights_by_uid), weights=list(weights_by_uid.values()),
            period=self.weight_period, raise_error=True, wait_for_inclusion=True,
        )
        if not getattr(response, "success", False):
            return {"status": "submission_failed", "round_id": bundle["round_id"],
                    "source": source, "current_block": block, "alert_stale": block - int(last_updates[uid]) >= 2000}
        self.last_submitted_block = block
        return {"status": "submitted", "round_id": bundle["round_id"],
                "source": source, "current_block": block, "weights": weights_by_uid}
