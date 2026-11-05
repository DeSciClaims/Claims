"""Resolve the predecessor once, after intake, while preserving admission rules."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .handoff_controller import fetch_round_publication
from .incumbent import resolve_baseline
from .production_handoff import planned_production_handoff
from .production_reader import ProductionReleaseReader
from .round_setup import _canonical_bytes, _write_once


def deferred(record):
    return record.get("rules_public", {}).get("policy", {}).get("baseline_selection") == "previous_completed_round"


def prepare_evaluation(row, backend, *, now=None):
    """Return a wait receipt, or durably bind this queue head's signed baseline."""
    revision = int(row.get("rules_revision", 1))
    if revision > 1:
        from .rule_amendments import sync_current_rules
        row = sync_current_rules(backend, row)
        revision = int(row["rules_revision"])
    if not deferred(row):
        return {"status": "evaluation_ready"}
    gate = backend.evaluation_gate(row["round_id"])
    if not gate["ready"]:
        return {"status": "waiting_for_preceding_round"}
    previous = gate.get("previous_round")
    if previous and previous["status"] == "failed" and previous.get("decision") != "no_contest":
        from .recurrence import read_json
        profile_path = os.environ.get("CLAIMS_COMPETITION_RECURRENCE_CONFIG")
        profile = read_json(Path(profile_path)) if profile_path else {}
        if previous["round_id"] != profile.get("acknowledged_failed_round_id"):
            return {"status": "waiting_for_preceding_failure_review"}
    root = Path(os.environ["CLAIMS_COMPETITION_ROUNDS_DIR"])
    suffix = f"evaluation-r{revision}" if revision > 1 else "evaluation"
    path = root / f"{row['round_id']}.{suffix}.json"
    if row.get("evaluation_binding") is not None:
        if revision > 1:
            from .pilot_worker_factory import _round_file
            if _round_file(row["round_id"]).get("binding") != row["evaluation_binding"]:
                raise ValueError("private revised evaluation receipt differs from backend binding")
            return {"status": "evaluation_ready"}
        if not path.is_file() or json.loads(path.read_text()).get("binding") != row["evaluation_binding"]:
            raise ValueError("private evaluation receipt differs from backend binding")
        return {"status": "evaluation_ready"}
    clock = now or datetime.now(UTC)
    reader = ProductionReleaseReader(Path(os.environ["CLAIMS_COMPETITION_PRODUCTION_ROOT"]))
    active = reader.read_active()
    predecessor = gate.get("predecessor")
    if predecessor is None:
        if active is not None:
            raise ValueError("active production has no signed predecessor")
    else:
        rid = predecessor["round_id"]
        if active is None or active.round_id != rid or clock < active.latest_activation_at:
            return {"status": "waiting_for_preceding_handoff"}
        prior = backend.get_round(rid)
        stages = backend.list_stages(rid)
        if not stages:
            raise ValueError("signed predecessor has no evaluation stage")
        receipt = fetch_round_publication(backend_url=backend.base_url, round_id=rid)
        plan = planned_production_handoff(
            receipt=receipt, round_record=prior, final_stage=stages[-1],
            expected_signer_hotkey=os.environ["CLAIMS_COMPETITION_PUBLISHER_HOTKEY"],
            network=row["network"], now=datetime.max.replace(tzinfo=UTC),
        )
        if (plan is None or active is None or clock < plan.activate_at
            or any(getattr(active, k) != getattr(plan, k) for k in (
                "round_id", "receipt_sha256", "hotkey", "package_sha256"))
            or active.latest_activation_at != plan.activate_at):
            return {"status": "waiting_for_preceding_handoff"}
    from .pilot_worker_factory import _round_file
    document = _round_file(row["round_id"])
    resolved = resolve_baseline(
        {**document, "network": row["network"], "deadline_at": row["deadline_at"],
         "baseline_selection": "active_production"},
        backend=backend, reader=reader,
        publication_for_round=lambda rid: fetch_round_publication(backend_url=backend.base_url, round_id=rid),
        expected_signer_hotkey=os.environ["CLAIMS_COMPETITION_PUBLISHER_HOTKEY"],
        baseline_upload_for_round=lambda rid: _round_file(rid)["baseline_upload"], as_of=clock,
    )
    binding = {
        "schema_version": "claims_competition_evaluation_binding_v1",
        **{k: row[k] for k in ("round_id", "network", "rules_sha256", "configuration_sha256", "reference_package_sha256")},
        "incumbent_hotkey": resolved["incumbent_hotkey"],
        "baseline_package_sha256": resolved["baseline_upload"]["package_sha256"],
        "baseline_origin": resolved.get("baseline_origin"),
    }
    private = {"binding": binding, "baseline_upload": resolved["baseline_upload"],
               "reference_upload": resolved.get("reference_upload"),
               "incumbent_hotkey": resolved["incumbent_hotkey"],
               "baseline_origin": resolved.get("baseline_origin"), "baseline_pending": False}
    _write_once(path, _canonical_bytes(private), 0o600)
    if backend.bind_evaluation(row["round_id"], binding) != binding:
        raise ValueError("backend changed evaluation binding")
    return {"status": "evaluation_ready"}
