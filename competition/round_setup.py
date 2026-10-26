"""Prepare immutable private round inputs and a publishable weekly rules file."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from .controller_inputs import build_provisional_evaluation_policy
from .code_audit import validated_audit_policy
from .provisional_judges import require_provisional_provider_pins
from .publication import canonical_sha256


RULES_SCHEMA = "claims_competition_rules_v1"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode()


def _write_once(path: Path, content: bytes, mode: int) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"round file {path.name} already differs")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".competition-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # O_EXCL avoids replacing a file created by another setup process.
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError(f"round file {path.name} already differs") from None
    finally:
        os.unlink(temporary)


def prepare_round(
    draft: Mapping[str, Any], *, backend: Any, directory: Path,
) -> dict[str, str]:
    """Freeze artifacts locally, then idempotently open the round in backend."""
    round_id = draft.get("round_id")
    network = draft.get("network")
    configuration = draft.get("configuration")
    baseline = draft.get("baseline_upload")
    reference = draft.get("reference_upload")
    if (not isinstance(round_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id)
        or network not in {"mainnet", "testnet"}
        or not isinstance(configuration, dict)
        or not isinstance(baseline, dict)
        or (reference is not None and not isinstance(reference, dict))):
        raise ValueError("weekly round draft is incomplete")
    deadline = datetime.fromisoformat(str(draft["deadline_at"]))
    if deadline.tzinfo is None:
        raise ValueError("weekly deadline needs a timezone")
    deadline = deadline.astimezone(UTC)
    if (deadline.weekday(), deadline.hour, deadline.minute, deadline.second,
        deadline.microsecond) != (4, 12, 0, 0, 0):
        raise ValueError("weekly deadline must be Friday 12:00 UTC")
    week_start = (deadline - timedelta(days=4)).date().isoformat()
    if str(draft.get("week_start")) != week_start:
        raise ValueError("week start differs from deadline week Monday")
    block = draft.get("deadline_block")
    reveal = draft.get("reveal_block")
    if (type(block) is not int or block < 0 or type(reveal) is not int
        or reveal < block + 600):
        raise ValueError("weekly reveal must be at least block D+600")
    incumbent = draft.get("incumbent_hotkey")
    if incumbent is not None and (not isinstance(incumbent, str) or not incumbent):
        raise ValueError("incumbent hotkey is invalid")
    for name, upload in (("baseline", baseline), ("reference", reference)):
        if upload is None:
            continue
        if (not isinstance(upload.get("package_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", upload["package_sha256"])
            or not upload.get("hotkey") or not upload.get("team_id")
            or not isinstance(upload.get("package_object_key"), str)
            or not upload["package_object_key"].startswith("sealed/")
            or ".." in upload["package_object_key"]
            or type(upload.get("package_size_bytes")) is not int
            or not 0 < upload["package_size_bytes"] <= 100_000_000
            or not isinstance(upload.get("manifest"), dict)):
            raise ValueError(f"{name} sealed upload receipt is incomplete")
    if incumbent is None and reference is not None:
        raise ValueError("week one uses the baseline as the reference")
    if incumbent is not None and (baseline["hotkey"] != incumbent or reference is None):
        raise ValueError("incumbent and fallback reference must be frozen")
    reference_hash = (baseline if incumbent is None else reference)["package_sha256"]
    release_id = draft.get("reference_release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("Bronze reference release ID is required")
    links = draft.get("linked_pairs", [])
    if (not isinstance(links, list)
        or any(not isinstance(pair, list) or len(pair) != 3
               or any(not isinstance(value, str) or not value for value in pair)
               for pair in links)):
        raise ValueError("reviewed duplicate links are malformed")
    if links:
        raise ValueError("duplicate links must be reviewed after the intake lock")
    public_policy = draft.get("policy_public", {})
    if not isinstance(public_policy, dict):
        raise ValueError("public weekly policy is malformed")
    reference_lock = public_policy.get("reference_lock_sha256")
    if not isinstance(reference_lock, str) or not re.fullmatch(r"[0-9a-f]{64}", reference_lock):
        raise ValueError("public weekly policy needs the frozen Bronze reference lock SHA-256")
    audit_policy = public_policy.get("code_audit")
    if network == "mainnet" and audit_policy is None:
        raise ValueError("mainnet round needs automatic code audit thresholds")
    if audit_policy is not None:
        validated_audit_policy(audit_policy)
    dispute_window = public_policy.get("duplicate_dispute_window")
    if dispute_window is None:
        raise ValueError("public duplicate dispute window is required")
    try:
        opened = datetime.fromisoformat(dispute_window["opens_at"])
        closed = datetime.fromisoformat(dispute_window["closes_at"])
        verdict_public_at = datetime.fromisoformat(public_policy["duplicate_verdict_public_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("duplicate verdict release or dispute window is malformed") from exc
    if (not isinstance(dispute_window, dict)
        or set(dispute_window) != {"opens_at", "closes_at"}
        or opened.tzinfo is None or closed.tzinfo is None
        or verdict_public_at.tzinfo is None
        or verdict_public_at.astimezone(UTC) != deadline + timedelta(days=4, hours=-3)
        or opened.astimezone(UTC) != verdict_public_at.astimezone(UTC)
        or closed.astimezone(UTC) != deadline + timedelta(days=4, hours=3)):
        raise ValueError("duplicate verdict and dispute window must be Tuesday 09:00–15:00 UTC")
    config_hash = canonical_sha256(configuration)
    rules = {
        "schema_version": RULES_SCHEMA,
        "round_id": round_id, "network": network,
        "week_start": week_start,
        "deadline_at": deadline.isoformat(),
        "deadline_block": block, "reveal_block": reveal,
        "configuration_sha256": config_hash,
        "configuration": configuration,
        "reference_release_id": release_id,
        "incumbent_hotkey": incumbent,
        "baseline_package_sha256": baseline["package_sha256"],
        "reference_package_sha256": reference_hash,
        "policy": public_policy,
    }
    rules_hash = canonical_sha256(rules)
    private = {
        "round_id": round_id,
        "configuration_sha256": config_hash,
        "configuration": configuration,
        "baseline_upload": baseline,
        "reference_upload": reference,
        "linked_pairs": links,
    }
    if not isinstance(draft.get("policy_public", {}), dict):
        raise ValueError("public policy must be a JSON object")
    if not directory.is_dir():
        raise ValueError("trusted round directory must exist")
    _write_once(directory / f"{round_id}.json", _canonical_bytes(private), 0o600)
    _write_once(directory / f"{round_id}.rules.json", _canonical_bytes(rules), 0o644)
    payload = {key: rules[key] for key in (
        "round_id", "network", "week_start", "deadline_at", "deadline_block",
        "reveal_block", "configuration_sha256", "reference_release_id",
        "incumbent_hotkey", "baseline_package_sha256", "reference_package_sha256",
    )}
    payload["rules_sha256"] = rules_hash
    payload["rules_public"] = rules
    stored = backend.create_round(payload)
    if (stored.get("round_id") != round_id or stored.get("rules_sha256") != rules_hash
        or stored.get("configuration_sha256") != config_hash
        or stored.get("rules_public") != rules):
        raise ValueError("backend did not preserve frozen round setup")
    return {"round_id": round_id, "rules_sha256": rules_hash,
            "configuration_sha256": config_hash}


def prepare_pilot_round(
    draft: Mapping[str, Any], *, backend: Any, directory: Path,
    storage: Any, decision_api_key: str,
) -> dict[str, str]:
    """Verify the actual judge runtime and sealed baselines before opening intake."""
    configuration = draft.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("pilot round needs a complete configuration")
    routing = configuration.get("decision_routing")
    if not isinstance(routing, dict):
        raise ValueError("pilot round has no frozen Decision routing")
    policy = build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(round_id=draft.get("round_id"),
                                     source_context_for_paper=lambda _: {}),
        decision_api_key=decision_api_key,
        weekly_budget_usd=float(configuration["weekly_budget_usd"]),
        worker_policy=configuration["worker_policy"],
        prescreen_threshold=float(routing["prescreen_confidence_threshold"]),
        screen_threshold=float(routing["screen_confidence_threshold"]),
        decision_api_base=str(routing["api_base"]),
    )
    if policy.configuration != configuration:
        raise ValueError("pilot judge runtime differs from proposed frozen rules")
    require_provisional_provider_pins(configuration)
    for upload in (draft.get("baseline_upload"), draft.get("reference_upload")):
        if upload is not None:
            storage.package_for_submission(upload)
    return prepare_round(draft, backend=backend, directory=directory)
