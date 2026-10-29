"""Freeze the activated, signed production release as a new round's baseline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from .publication import FROZEN_FIELDS, canonical_sha256
from .weight_heartbeat import verify_weekly_receipt


def resolve_baseline(
    config: Mapping[str, Any],
    *,
    backend: Any,
    reader: Any,
    publication_for_round: Callable[[str], Mapping[str, Any]],
    expected_signer_hotkey: str,
) -> dict[str, Any]:
    if config.get("baseline_selection") != "active_production":
        return dict(config)
    reference = config.get("reference_upload") or config.get("baseline_upload")
    if not isinstance(reference, dict):
        raise ValueError(
            "automatic baseline selection needs a sealed fallback reference"
        )
    active = reader.read_active()
    if active is None:
        return {
            **config,
            "incumbent_hotkey": None,
            "baseline_upload": reference,
            "reference_upload": None,
        }
    prior = backend.get_round(active.round_id)
    receipt = publication_for_round(active.round_id)
    bundle = verify_weekly_receipt(
        receipt,
        expected_signer_hotkey=expected_signer_hotkey,
        network=config["network"],
    )
    if (
        prior.get("status") != "published"
        or any(prior.get(key) != bundle.get(key) for key in FROZEN_FIELDS)
        or canonical_sha256(prior.get("rules_public")) != bundle["rules_sha256"]
        or active.receipt_sha256 != receipt.get("bundle_sha256")
        or active.activate_at > datetime.fromisoformat(config["deadline_at"])
    ):
        raise ValueError("active package differs from its prior signed round")
    results = bundle["results"]
    if results.get("decision") in {"reference_fallback", "no_crown"}:
        if reference.get("package_sha256") != active.package_sha256:
            raise ValueError(
                "active fallback reference differs from configured reference"
            )
        return {
            **config,
            "incumbent_hotkey": None,
            "baseline_upload": reference,
            "reference_upload": None,
        }
    if not any(
        row.get("challenger_hotkey") == active.hotkey
        and row.get("challenger_package_sha256") == active.package_sha256
        for row in results.get("final_pair_scores", [])
    ):
        raise ValueError("active package differs from signed winning package")
    if results.get("production_hotkey") != active.hotkey:
        raise ValueError("active hotkey differs from signed selection")
    entries = [
        entry
        for stage in backend.list_stages(active.round_id)
        for entry in stage["entries"]
        if entry["hotkey"] == active.hotkey
        and entry["package_sha256"] == active.package_sha256
    ]
    uploads = [
        row
        for row in backend.list_submissions(active.round_id)
        if row["hotkey"] == active.hotkey
        and row["package_sha256"] == active.package_sha256
        and row["status"] in {"accepted", "evaluating", "finished"}
    ]
    if (
        len(uploads) != 1
        or not entries
        or any(row["team_id"] != uploads[0]["team_id"] for row in entries)
    ):
        raise ValueError("active package has no unique sealed submission and team")
    return {
        **config,
        "incumbent_hotkey": active.hotkey,
        "baseline_upload": uploads[0],
        "reference_upload": reference,
        "baseline_origin": {
            "round_id": active.round_id,
            "receipt_sha256": active.receipt_sha256,
        },
    }
