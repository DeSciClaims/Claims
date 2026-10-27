"""Verify the Thursday result before scheduling the next Monday package."""

from __future__ import annotations

import re
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .publication import FROZEN_FIELDS, canonical_sha256
from .round_schedule import schedule_for_round
from .weight_heartbeat import verify_weekly_receipt


@dataclass(frozen=True)
class ProductionHandoffPlan:
    round_id: str
    receipt_sha256: str
    hotkey: str
    package_sha256: str
    activate_at: datetime


class FilesystemProductionReleaseStore:
    """Atomically record a verified package and active receipt on trusted disk.

    The production service reads ``active.json`` to load the package; this
    class stores bytes and the pointer but does not execute miner code.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if (self.root.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("production release root must be a private owned directory")

    def _write_atomic(self, path: Path, content: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".release-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                os.fchmod(temporary.fileno(), 0o600)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(temporary_path, path)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary_path.unlink(missing_ok=True)

    def _verified_package(self, digest: Any) -> Path:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("stored production package hash is invalid")
        path = self.root / f"{digest}.zip"
        if (path.is_symlink() or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("stored production package is missing or corrupt")
        return path

    def activate(
        self, plan: ProductionHandoffPlan, package: bytes, *, now: datetime,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if now.tzinfo is None or now.astimezone(UTC) < plan.activate_at:
            raise ValueError("production activation is before the verified gate")
        if (hashlib.sha256(package).hexdigest() != plan.package_sha256
            or not re.fullmatch(r"[0-9a-f]{64}", plan.receipt_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", plan.package_sha256)):
            raise ValueError("production package or receipt differs from verified handoff")
        if manifest is not None:
            if not isinstance(manifest, dict):
                raise ValueError("production manifest must be a JSON object")
            try:
                manifest_bytes = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ).encode()
            except (TypeError, ValueError) as exc:
                raise ValueError("production manifest is not canonical JSON") from exc
            if len(manifest_bytes) > 12_000:
                raise ValueError("production manifest exceeds the release limit")
            manifest_sha256 = canonical_sha256(manifest)
        else:
            manifest_sha256 = None
        lock_path = self.root / ".activation.lock"
        if lock_path.is_symlink():
            raise ValueError("production activation lock cannot be a symlink")
        with lock_path.open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            active_path = self.root / "active.json"
            if active_path.is_symlink():
                raise ValueError("production active pointer cannot be a symlink")
            current = json.loads(active_path.read_text()) if active_path.exists() else None
            if current is not None:
                self._verified_package(current["package_sha256"])
                if current["receipt_sha256"] == plan.receipt_sha256:
                    if (current["package_sha256"] != plan.package_sha256
                        or current.get("manifest_sha256") != manifest_sha256):
                        raise ValueError("active receipt has a different package")
                    return current
                current_time = datetime.fromisoformat(
                    current.get("latest_activation_at", current["activate_at"]),
                )
                if current_time >= plan.activate_at:
                    raise ValueError("production handoff is older than the active release")
            package_path = self.root / f"{plan.package_sha256}.zip"
            if package_path.exists():
                self._verified_package(plan.package_sha256)
            else:
                self._write_atomic(package_path, package)
            activation = {
                "round_id": plan.round_id,
                "receipt_sha256": plan.receipt_sha256,
                "hotkey": plan.hotkey,
                "package_sha256": plan.package_sha256,
                "manifest": manifest,
                "manifest_sha256": manifest_sha256,
                "activate_at": plan.activate_at.isoformat(),
                "latest_activation_at": plan.activate_at.isoformat(),
                "previous": ({key: value for key, value in current.items()
                             if key != "previous"} if current is not None else None),
            }
            self._write_atomic(active_path, json.dumps(
                activation, sort_keys=True, separators=(",", ":"),
            ).encode())
            return activation

    def rollback(
        self, *, expected_receipt_sha256: str, reason: str, now: datetime,
    ) -> dict[str, Any]:
        """Restore the previous sealed package after a verified production breach."""
        if (not re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256)
            or not reason.strip() or len(reason) > 500
            or now.tzinfo is None):
            raise ValueError("rollback needs current receipt, reason and timezone-aware clock")
        lock_path = self.root / ".activation.lock"
        if lock_path.is_symlink():
            raise ValueError("production activation lock cannot be a symlink")
        with lock_path.open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            active_path = self.root / "active.json"
            if active_path.is_symlink() or not active_path.exists():
                raise ValueError("production active pointer is missing or unsafe")
            current = json.loads(active_path.read_text())
            if current.get("rolled_back_from") == expected_receipt_sha256:
                return current
            if current.get("receipt_sha256") != expected_receipt_sha256:
                raise ValueError("rollback target is not the current production receipt")
            previous = current.get("previous")
            if not isinstance(previous, dict):
                raise ValueError("production has no previous release to restore")
            self._verified_package(previous.get("package_sha256"))
            restored = {
                **previous,
                "latest_activation_at": current.get("latest_activation_at", current["activate_at"]),
                "rolled_back_from": expected_receipt_sha256,
                "rollback_at": now.astimezone(UTC).isoformat(),
                "rollback_reason": reason.strip(),
            }
            self._write_atomic(active_path, json.dumps(
                restored, sort_keys=True, separators=(",", ":"),
            ).encode())
            return restored


def activate_verified_handoff(
    *, receipt: Mapping[str, Any], round_record: Mapping[str, Any],
    final_stage: Mapping[str, Any], expected_signer_hotkey: str,
    network: str, now: datetime,
    fetch_package: Callable[[ProductionHandoffPlan], bytes],
    releases: FilesystemProductionReleaseStore,
) -> dict[str, Any] | None:
    """Fetch exactly the selected sealed package, then move the local pointer."""
    plan = planned_production_handoff(
        receipt=receipt, round_record=round_record, final_stage=final_stage,
        expected_signer_hotkey=expected_signer_hotkey, network=network, now=now,
    )
    if plan is None:
        return None
    return releases.activate(plan, fetch_package(plan), now=now)


def planned_production_handoff(
    *, receipt: Mapping[str, Any], round_record: Mapping[str, Any],
    final_stage: Mapping[str, Any], expected_signer_hotkey: str,
    network: str, now: datetime,
) -> ProductionHandoffPlan | None:
    """Return the exact winning package once its activation gate is due.

    Continuing the incumbent and a week-one no-crown result need no package
    change. The caller may stage the returned sealed package in trusted
    production infrastructure, using the receipt digest as an idempotency key.
    """
    if now.tzinfo is None:
        raise ValueError("handoff clock must have a timezone")
    bundle = verify_weekly_receipt(
        receipt, expected_signer_hotkey=expected_signer_hotkey, network=network,
    )
    if (round_record.get("status") != "published"
        or any(round_record.get(field) != bundle.get(field) for field in FROZEN_FIELDS)):
        raise ValueError("handoff round differs from signed publication")
    rules = round_record.get("rules_public")
    if (not isinstance(rules, dict)
        or canonical_sha256(rules) != bundle["rules_sha256"]
        or rules.get("round_id") != bundle["round_id"]
        or rules.get("network") != bundle["network"]):
        raise ValueError("handoff rules differ from signed publication")
    try:
        deadline = datetime.fromisoformat(str(rules["deadline_at"]))
        stored_deadline = datetime.fromisoformat(str(round_record["deadline_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("handoff deadline is malformed") from exc
    if deadline.tzinfo is None or stored_deadline.tzinfo is None:
        raise ValueError("handoff deadline must have a timezone")
    deadline = deadline.astimezone(UTC)
    if deadline != stored_deadline.astimezone(UTC):
        raise ValueError("handoff deadline differs from signed rules")
    activate_at = schedule_for_round(round_record).activate_at
    if now.astimezone(UTC) < activate_at:
        return None
    results = bundle["results"]
    decision = results.get("decision")
    if decision == "incumbent_continues":
        expected_hotkey = round_record.get("incumbent_hotkey")
        if (not expected_hotkey or results.get("production_hotkey") != expected_hotkey):
            raise ValueError("no-change decision names another production hotkey")
        return None
    if decision in {"reference_fallback", "no_crown"}:
        baseline = [row for row in final_stage.get("entries", [])
                    if row.get("role") == "reference"]
        hotkey = (results.get("fallback_reference_hotkey")
                  if decision == "reference_fallback" else baseline[0].get("hotkey")
                  if len(baseline) == 1 else None)
        roles = bundle["weights"].get("roles", [])
        pairs = results.get("final_pair_scores")
        security = results.get("worker_security_audit", {})
        if ((decision == "reference_fallback") != bool(round_record.get("incumbent_hotkey"))
            or results.get("production_hotkey") is not None
            or len(baseline) != 1 or hotkey != baseline[0].get("hotkey")
            or baseline[0].get("package_sha256") != bundle["reference_package_sha256"]
            or (decision == "no_crown" and final_stage.get("stage") != "week_one")
            or (decision == "reference_fallback"
                and final_stage.get("stage") not in {"final", "rerun"})
            or (final_stage.get("stage") == "rerun"
                and final_stage.get("source_stage") != "final")
            or not isinstance(roles, list)
            or not any(row.get("role") == "production_burned" and row.get("share") == 0.07
                       for row in roles)
            or any(row.get("role") == "production" for row in roles)
            or not isinstance(security, dict) or security.get(hotkey, {}).get("passed") is not True
            or not isinstance(pairs, list) or not pairs
            or any(row.get("stage") != final_stage["stage"]
                   or row.get("baseline_hotkey") != hotkey
                   or row.get("baseline_package_sha256") != bundle["reference_package_sha256"]
                   for row in pairs)):
            raise ValueError("reference production differs from signed final evidence")
        return ProductionHandoffPlan(
            round_id=bundle["round_id"], receipt_sha256=receipt["bundle_sha256"],
            hotkey=hotkey, package_sha256=bundle["reference_package_sha256"],
            activate_at=activate_at,
        )
    if decision not in {"new_crown", "incumbent_version_swap"}:
        raise ValueError("handoff decision is unknown")
    hotkey = results.get("production_hotkey")
    if not isinstance(hotkey, str) or not hotkey:
        raise ValueError("signed winner has no production hotkey")
    if decision == "new_crown" and results.get("winner_hotkey") != hotkey:
        raise ValueError("new crown differs from signed winner hotkey")
    roles = bundle["weights"]["roles"]
    paid = [row for row in roles if row["role"].removesuffix("_burned") == "production"]
    if len(paid) != 1 or paid[0]["hotkey"] != hotkey:
        raise ValueError("handoff winner differs from signed production role")
    stage = final_stage.get("stage")
    if stage not in {"final", "week_one", "rerun"} or (
        stage == "rerun" and final_stage.get("source_stage") != "final"
    ):
        raise ValueError("handoff stage differs from frozen final stage")
    entries = [row for row in final_stage.get("entries", [])
               if row.get("hotkey") == hotkey and row.get("role") == "challenger"]
    if len(entries) != 1:
        raise ValueError("handoff winner has no unique frozen finalist package")
    baseline = [row for row in final_stage["entries"]
                if row.get("role") in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("handoff baseline differs from signed round")
    expected_baseline_hash = (bundle["reference_package_sha256"]
                              if baseline[0]["role"] == "reference"
                              else bundle["baseline_package_sha256"])
    if (baseline[0].get("package_sha256") != expected_baseline_hash
        or (baseline[0]["role"] == "incumbent"
            and baseline[0].get("hotkey") != round_record["incumbent_hotkey"])):
        raise ValueError("handoff baseline differs from signed round")
    same_team = entries[0].get("team_id") == baseline[0].get("team_id")
    if not entries[0].get("team_id") or not baseline[0].get("team_id") or same_team != (decision == "incumbent_version_swap"):
        raise ValueError("handoff decision differs from frozen team relationship")
    package_sha256 = entries[0].get("package_sha256")
    if not isinstance(package_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", package_sha256):
        raise ValueError("handoff finalist package hash is invalid")
    pair_grid = results.get("final_pair_scores")
    if not isinstance(pair_grid, list) or any(not isinstance(row, dict) for row in pair_grid):
        raise ValueError("handoff final paper grid is malformed")
    rows = [row for row in pair_grid
            if row.get("challenger_hotkey") == hotkey]
    if (not rows or any(row.get("round_id") != bundle["round_id"]
                        or row.get("stage") != stage
                        or row.get("challenger_package_sha256") != package_sha256
                        or row.get("configuration_sha256") != bundle["configuration_sha256"]
                        for row in rows)):
        raise ValueError("handoff package differs from signed final paper grid")
    return ProductionHandoffPlan(
        round_id=bundle["round_id"], receipt_sha256=receipt["bundle_sha256"],
        hotkey=hotkey, package_sha256=package_sha256, activate_at=activate_at,
    )
