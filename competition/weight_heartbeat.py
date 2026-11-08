"""Verify a weekly publication and project its roles to current chain UIDs."""

from __future__ import annotations

import math
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
import fcntl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from bittensor_wallet import Keypair

from .publication import FROZEN_FIELDS, PUBLICATION_SCHEMA, canonical_sha256
from .replay_publication import REPLAY_PUBLICATION_SCHEMA
from .legacy_weight_carry import verify_legacy_weights, project_legacy_weights

ROLE_SHARES = {
    "operator": 0.90,
    "production": 0.07,
    "runner_up_1": 0.02,
    "runner_up_2": 0.01,
}


def verify_weekly_receipt(
    receipt: Mapping[str, Any], *, expected_signer_hotkey: str, network: str,
    independent: bool = False,
) -> dict[str, Any]:
    """Reject altered, unsigned, cross-network, or malformed publications."""
    if network not in {"testnet", "mainnet"} or not expected_signer_hotkey:
        raise ValueError("expected signer and competition network are required")
    bundle = receipt.get("bundle")
    schema = REPLAY_PUBLICATION_SCHEMA if independent else PUBLICATION_SCHEMA
    if not isinstance(bundle, dict) or bundle.get("schema_version") != schema:
        raise ValueError("invalid weekly publication schema")
    frozen_fields = (
        "round_id", "network", "archive_block_hash", "drand_round", "drand_seed",
        "paper_pool_sha256", "duplicate_review_sha256", "rules_sha256",
        "configuration_sha256", "baseline_package_sha256",
        "reference_package_sha256",
    ) if independent else FROZEN_FIELDS
    if (bundle.get("network") != network
        or any(bundle.get(field) is None for field in frozen_fields)
        or not isinstance(bundle.get("results"), dict)
        or not bundle["results"].get("decision")):
        raise ValueError("weekly publication is for another network or no round")
    if independent and (
        type(bundle.get("drand_round")) is not int or bundle["drand_round"] < 1
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(bundle.get(field, "")))
               for field in (
                   "local_sample_sha256", "local_pairs_sha256",
                   "gold_adjusted_pairs_sha256", "local_final_audit_input_sha256",
                   "local_final_audit_sha256", "gold_evidence_sha256",
                   "qualification_evidence_sha256",
               ))
    ):
        raise ValueError("independent weekly evidence is malformed")
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


def fetch_latest_publication(
    *, backend_url: str, network: str, independent: bool = False,
) -> dict[str, Any] | None:
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
    for published in rounds:
        if published.get("status") != "published":
            continue
        round_id = str(published["round_id"])
        suffix = "/independent-results" if independent else "/results"
        try:
            with urllib.request.urlopen(
                base + "/public/competition/rounds/"
                + urllib.parse.quote(round_id, safe="") + suffix,
                timeout=15,
            ) as response:
                receipt = json.load(response)
        except urllib.error.HTTPError as exc:
            if independent and exc.code == 404:
                continue
            raise
        if not isinstance(receipt, dict):
            raise ValueError("competition publication is malformed")
        return receipt
    return None


class WeeklyWeightHeartbeat:
    """Refresh a verified allocation on a configurable wall-clock interval."""

    def __init__(
        self, *, network: str, netuid: int, backend_url: str,
        operator_hotkey: str, wallet: Any, subtensor: Any,
        cache_path: Path, weight_period: int = 16,
        fetcher: Callable[[], dict[str, Any] | None] | None = None,
        independent: bool = False,
        submission_interval_seconds: int | None = None,
        legacy_receipt_path: Path | None = None,
        state_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
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
        self.independent = independent
        if (submission_interval_seconds is not None
            and (type(submission_interval_seconds) is not int or submission_interval_seconds < 1)):
            raise ValueError("weight submission interval must be a positive integer")
        if independent and legacy_receipt_path is not None:
            raise ValueError("legacy carry-forward is only supported by the primary heartbeat")
        self.submission_interval_seconds = submission_interval_seconds
        self.legacy_receipt_path = legacy_receipt_path
        self.state_path = state_path or cache_path.with_name(cache_path.name + ".state.json")
        self.now = now or (lambda: datetime.now(UTC))
        self.expected_signer = wallet.hotkey.ss58_address
        self.fetcher = fetcher or (lambda: fetch_latest_publication(
            backend_url=backend_url, network=network, independent=independent,
        ))
        self.last_submitted_block = 0

    def _load_cache(self) -> dict[str, Any] | None:
        try:
            receipt = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        verify_weekly_receipt(
            receipt, expected_signer_hotkey=self.expected_signer, network=self.network,
            independent=self.independent,
        )
        return receipt

    def _save_cache(self, receipt: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(self.cache_path.name + ".tmp")
        temporary.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.cache_path)

    def _receipt_order(self, receipt: Mapping[str, Any]) -> int:
        field = "drand_round" if self.independent else "drand_scheduled_at"
        value = receipt["bundle"][field]
        if type(value) is not int or value < 1:
            raise ValueError("competition receipt ordering must be a positive integer")
        return value

    def _receipt_precedes_state(
        self, receipt: Mapping[str, Any], state: Mapping[str, Any], *, full_cache_available: bool,
    ) -> bool:
        digest = state.get("latest_competition_receipt_sha256")
        if digest is None:
            return False
        order = state.get("latest_competition_order")
        if order is None:
            # Older state can recover its exact signed receipt. Without that
            # receipt's ordering evidence, a different digest cannot be ranked.
            return not full_cache_available and receipt["bundle_sha256"] != digest
        candidate_order = self._receipt_order(receipt)
        return (candidate_order < order
                or (candidate_order == order and receipt["bundle_sha256"] != digest))

    def _current_receipt(self, state: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        cached = self._load_cache()
        # A full cache establishes missing ordering evidence only when its
        # digest matches the receipt already remembered by older state.
        if cached is not None and self._receipt_precedes_state(cached, state, full_cache_available=False):
            raise RuntimeError("cached competition receipt conflicts with persisted publication order")
        try:
            fetched = self.fetcher()
            if fetched is None:
                return cached, "cached" if cached else "unpublished"
            verify_weekly_receipt(
                fetched, expected_signer_hotkey=self.expected_signer, network=self.network,
                independent=self.independent,
            )
            new_time = self._receipt_order(fetched)
        except OSError as exc:
            if cached is None and self.legacy_receipt_path is not None:
                return None, "legacy_after_fetch_error"
            if cached is None:
                raise RuntimeError("no valid weekly receipt is available") from exc
            return cached, "cached_after_fetch_error"
        except (ValueError, KeyError, TypeError) as exc:
            if cached is None:
                raise RuntimeError("no valid weekly receipt is available") from exc
            return cached, "cached_after_fetch_error"
        if self._receipt_precedes_state(fetched, state, full_cache_available=cached is not None):
            if cached is not None:
                return cached, "cached_after_rollback"
            raise RuntimeError("competition receipt rollback or unknown ordering after cache loss")
        if cached is not None:
            old_time = self._receipt_order(cached)
            if new_time < old_time or (new_time == old_time and fetched["bundle_sha256"] != cached["bundle_sha256"]):
                return cached, "cached_after_rollback"
            if fetched["bundle_sha256"] == cached["bundle_sha256"]:
                return cached, "cached_current"
        self._save_cache(fetched)
        return fetched, "fresh"

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}
        if (not isinstance(state, dict)
            or state.get("network") != self.network or state.get("netuid") != self.netuid
            or state.get("signer_hotkey") != self.expected_signer):
            raise ValueError("heartbeat submission state belongs to another signer or network")
        for key in ("last_submitted_block",):
            if key in state and (type(state[key]) is not int or state[key] < 0):
                raise ValueError("invalid persisted heartbeat block")
        if "last_submitted_at" in state:
            _utc_time(state["last_submitted_at"])
        if "latest_competition_receipt_sha256" in state and not re.fullmatch(
            r"[0-9a-f]{64}", str(state["latest_competition_receipt_sha256"]),
        ):
            raise ValueError("invalid persisted competition receipt digest")
        if "latest_competition_order" in state and (
            type(state["latest_competition_order"]) is not int
            or state["latest_competition_order"] < 1
            or "latest_competition_receipt_sha256" not in state
        ):
            raise ValueError("invalid persisted competition publication order")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        state = {**state, "network": self.network, "netuid": self.netuid,
                 "signer_hotkey": self.expected_signer}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def step(self, *, audit_only: bool = False) -> dict[str, Any]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.with_name(self.state_path.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "heartbeat_busy"}
            return self._step(audit_only=audit_only)

    def _step(self, *, audit_only: bool) -> dict[str, Any]:
        now = _utc_time(self.now())
        state = self._load_state()
        receipt, source = self._current_receipt(state)
        legacy_bundle = None
        if receipt is None:
            if self.legacy_receipt_path is None:
                return {"status": "waiting_for_publication", "source": source}
            if (state.get("allocation_kind") == "competition"
                or state.get("latest_competition_receipt_sha256")):
                raise RuntimeError("verified competition receipt missing; refusing legacy rollback")
            receipt = json.loads(self.legacy_receipt_path.read_text())
            legacy_bundle = verify_legacy_weights(
                receipt, expected_signer_hotkey=self.expected_signer,
                network=self.network, netuid=self.netuid,
            )
            bundle = legacy_bundle
            kind = "legacy_extraction"
            if source == "unpublished":
                source = "legacy_carry_forward"
        else:
            bundle = receipt["bundle"]
            kind = "competition"
            order = self._receipt_order(receipt)
            if (state.get("latest_competition_receipt_sha256") != receipt["bundle_sha256"]
                or state.get("latest_competition_order") != order):
                # Audit already persists the verified receipt cache. Persist
                # this latch too so a lost cache cannot revive legacy payouts.
                state["latest_competition_receipt_sha256"] = receipt["bundle_sha256"]
                state["latest_competition_order"] = order
                self._save_state(state)
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
        chain_last_update = int(last_updates[uid])
        last_update = max(chain_last_update, self.last_submitted_block,
                          state.get("last_submitted_block", 0))
        if block < last_update:
            raise ValueError("chain block precedes validator's last weight update")
        tempo = int(info.tempo)
        if tempo < 1:
            raise ValueError("subnet tempo is invalid")
        due_after = min(tempo, 1800)
        if legacy_bundle is not None:
            if (not state.get("last_submitted_at") and not state.get("pending")
                and chain_last_update > legacy_bundle["last_update_block"]):
                raise ValueError("chain submission changed after legacy capture; reconcile before starting")
            weights_by_uid = project_legacy_weights(
                bundle, hotkeys_by_uid=hotkeys, owner_hotkey=str(info.owner_hotkey),
            )
        else:
            weights_by_uid = project_weekly_weights(
                bundle, hotkeys_by_uid=hotkeys, owner_hotkey=str(info.owner_hotkey),
                operator_hotkey=self.operator_hotkey,
            )
        allocation_id = receipt["bundle_sha256"]
        result = {"allocation_kind": kind, "source": source, "current_block": block,
                  "weights": weights_by_uid}
        if kind == "competition":
            result["round_id"] = bundle["round_id"]
        else:
            result["legacy_receipt_sha256"] = allocation_id

        pending = state.get("pending")
        if pending:
            if (not isinstance(pending, dict)
                or type(pending.get("before_block")) is not int
                or type(pending.get("attempt_block")) is not int
                or not re.fullmatch(r"[0-9a-f]{64}", str(pending.get("allocation_id", "")))):
                raise ValueError("invalid pending heartbeat submission")
            if (chain_last_update > pending["before_block"]
                and chain_last_update >= pending["attempt_block"]):
                try:
                    accepted_at = _utc_time(self.subtensor.get_timestamp(block=chain_last_update))
                except Exception:
                    # A pruned timestamp must not trigger a duplicate. Waiting
                    # six hours from reconciliation is the conservative anchor.
                    accepted_at = now
                state.update(last_submitted_block=chain_last_update,
                             last_submitted_at=accepted_at.isoformat(),
                             allocation_id=pending["allocation_id"],
                             allocation_kind=pending["allocation_kind"])
                state.pop("pending")
                if not audit_only:
                    self._save_state(state)
            else:
                expires_after = pending.get("reconcile_after_block")
                if expires_after is None:
                    # The process may have died during the RPC. Start a full
                    # conservative mortality window at the recovery head.
                    expires_after = block + 2 * max(4, self.weight_period)
                    pending["reconcile_after_block"] = expires_after
                    if not audit_only:
                        self._save_state(state)
                if type(expires_after) is not int or expires_after < pending["attempt_block"]:
                    raise ValueError("invalid pending heartbeat expiry")
                if block <= expires_after:
                    if not audit_only:
                        return {**result, "status": "waiting_for_submission_confirmation"}
                elif not audit_only:
                    # The mortality window expired with no accepted update.
                    state.pop("pending")
                    self._save_state(state)

        if self.submission_interval_seconds is not None:
            # Apply a newly published allocation promptly without depending on
            # historical state availability. The SDK still enforces chain rate
            # limits and commit/reveal rules for the actual transaction.
            new_competition = kind == "competition" and state.get("allocation_id") != allocation_id
            anchors = []
            if legacy_bundle is not None:
                anchors.append(_utc_time(legacy_bundle["last_update_at"]))
            if state.get("last_submitted_at"):
                anchors.append(_utc_time(state["last_submitted_at"]))
            if (not new_competition and chain_last_update
                and chain_last_update > state.get("last_submitted_block", 0)):
                if (legacy_bundle is not None
                    and chain_last_update == legacy_bundle["last_update_block"]):
                    anchors.append(_utc_time(legacy_bundle["last_update_at"]))
                else:
                    anchors.append(_utc_time(self.subtensor.get_timestamp(block=chain_last_update)))
            anchor = max(anchors) if anchors else now - timedelta(seconds=self.submission_interval_seconds)
            due_at = anchor + timedelta(seconds=self.submission_interval_seconds)
            # A newly published allocation takes effect promptly; recurring
            # refreshes of that allocation then follow the six-hour interval.
            result["due_at"] = (now if new_competition else due_at).isoformat()
            due = new_competition or now >= due_at
        else:
            result["due_block"] = last_update + due_after
            due = block - last_update >= due_after
        if audit_only:
            return {**result, "status": "audit_only"}
        if not due:
            return {**result, "status": "waiting_for_interval" if self.submission_interval_seconds is not None
                    else "waiting_for_tempo", "alert_stale": block - chain_last_update >= 2000}
        state["pending"] = {"before_block": chain_last_update, "attempt_block": block,
                            "attempt_at": now.isoformat(), "allocation_id": allocation_id,
                            "allocation_kind": kind}
        self._save_state(state)
        try:
            response = self.subtensor.set_weights(
                wallet=self.wallet, netuid=self.netuid,
                uids=list(weights_by_uid), weights=list(weights_by_uid.values()),
                period=self.weight_period, raise_error=True, wait_for_inclusion=True,
                max_attempts=1,
            )
        except Exception:
            try:
                head = int(self.subtensor.get_current_block())
                state["pending"]["reconcile_after_block"] = head + 2 * max(4, self.weight_period)
                self._save_state(state)
            except Exception:
                # The pre-RPC intent remains durable. Recovery will reserve a
                # fresh mortality window before considering another attempt.
                pass
            raise
        if not getattr(response, "success", False):
            state.pop("pending")
            self._save_state(state)
            return {**result, "status": "submission_failed", "alert_stale": block - chain_last_update >= 2000}
        accepted_block = int(self.subtensor.get_current_block())
        accepted_at = _utc_time(self.now())
        self.last_submitted_block = accepted_block
        state.pop("pending")
        state.update(last_submitted_block=accepted_block, last_submitted_at=accepted_at.isoformat(),
                     allocation_id=allocation_id, allocation_kind=kind)
        data = getattr(response, "data", None)
        if isinstance(data, dict):
            state["commit_metadata"] = {key: data[key] for key in ("reveal_round", "extrinsic_hash")
                                        if key in data and isinstance(data[key], (str, int))}
        self._save_state(state)
        return {**result, "status": "submitted", "accepted_at": accepted_at.isoformat(),
                "accepted_block": accepted_block, "commit_metadata": state.get("commit_metadata", {})}


def _utc_time(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("heartbeat timestamp must include a timezone")
    return parsed.astimezone(UTC)
