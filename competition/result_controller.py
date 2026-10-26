"""Turn an audited final selection into the signed public weekly result."""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any, Mapping, Protocol

from .final_selection import FinalSelection
from .final_stage import effective_final_stage
from .gold import GoldResult
from .judge_diagnostics import final_judge_diagnostics
from .publication import publication_bundle, sign_publication
from .weights import Payee, weekly_weight_plan


class PublicationBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def worker_inference_cost_summary(self, round_id: str) -> dict[str, Any]: ...
    def publish_round(self, round_id: str, signed_receipt: dict[str, Any]) -> dict[str, Any]: ...


def publish_selected_round(
    round_id: str, *, backend: PublicationBackend, signer: Any,
    selection: FinalSelection, operator_hotkey: str,
    uid_by_hotkey: Mapping[str, int], qualified_by_hotkey: Mapping[str, bool],
    production_team_id: str | None,
    gold_by_hotkey: Mapping[str, GoldResult],
    copy_audit: Mapping[str, Mapping[str, Any]],
    worker_security_audit: Mapping[str, Mapping[str, Any]],
    code_audit: Mapping[str, Any],
    gold_check: Mapping[str, Any] | None = None,
    known_answer_audit: Mapping[str, Any] | None = None,
    burn_uid: int = 0,
) -> dict[str, Any]:
    """Sign results and 70/20/10 shares; backend checks frozen inputs and grid."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold_closed"}:
        raise ValueError("round is not ready for final publication")
    finalist_hotkeys = {row.hotkey for row in selection.verdicts}
    if (set(copy_audit) != finalist_hotkeys
        or any(not isinstance(row.get("is_copy"), bool) for row in copy_audit.values())):
        raise ValueError("finalist output-copy evidence is incomplete")
    if (not finalist_hotkeys <= set(worker_security_audit)
        or any(not isinstance(row.get("passed"), bool)
               for row in worker_security_audit.values())):
        raise ValueError("final worker security audit is incomplete")
    if not finalist_hotkeys <= set(qualified_by_hotkey):
        raise ValueError("finalist qualification snapshot is incomplete")
    failed_checks = code_audit.get("failed_checks")
    digest = code_audit.get("evidence_sha256")
    if (not isinstance(failed_checks, dict) or set(failed_checks) != finalist_hotkeys
        or any(type(value) is not int or not 0 <= value <= 5
               for value in failed_checks.values())
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or any((row.reason == "code_failed") != (
            failed_checks[row.hotkey] > 0
            or not worker_security_audit[row.hotkey]["passed"]
        ) for row in selection.verdicts)):
        raise ValueError("final code audit counts differ from finalist verdicts")
    stages = backend.list_stages(round_id)
    effective_final_stage(stages)
    if {row["hotkey"] for row in stages[-1]["entries"] if row["role"] == "challenger"} != finalist_hotkeys:
        raise ValueError("publication finalists differ from frozen final stage")
    baseline = [row for row in stages[-1]["entries"] if row["role"] in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("frozen production baseline is missing")
    if (baseline[0]["role"] == "incumbent"
        and (baseline[0]["hotkey"] != round_record["incumbent_hotkey"]
             or baseline[0]["package_sha256"] != round_record["baseline_package_sha256"])):
        raise ValueError("frozen production baseline differs from round")
    if (baseline[0]["role"] == "reference"
        and baseline[0]["package_sha256"] != round_record["reference_package_sha256"]):
        raise ValueError("frozen reference baseline differs from round")
    if baseline[0]["role"] == "incumbent" and production_team_id != baseline[0]["team_id"]:
        raise ValueError("production team differs from frozen baseline")
    if selection.decision == "reference_fallback" and (
        baseline[0]["role"] != "reference" or selection.production_hotkey is not None
        or round_record["incumbent_hotkey"] is None
    ):
        raise ValueError("reference fallback decision differs from frozen final")
    finalists = [Payee(
        row.hotkey, row.team_id, qualified_by_hotkey[row.hotkey], row.mean_gain,
        copy=row.reason == "copy", code_passed=row.reason != "code_failed",
    ) for row in selection.verdicts]
    if set(gold_by_hotkey) != finalist_hotkeys and round_record["status"] == "gold_closed":
        raise ValueError("closed Gold results must cover every finalist")
    if round_record["status"] == "gold_closed" and not selection.final_pair_rows:
        raise ValueError("closed Gold publication needs post-escalation final pairs")
    if round_record["status"] == "gold_closed" and not gold_check:
        raise ValueError("closed Gold publication needs the committed check proof")
    if round_record["status"] == "final" and gold_by_hotkey:
        raise ValueError("unclosed Gold results cannot be published")
    production = None
    if selection.production_hotkey:
        verdict = next((row for row in selection.verdicts if row.hotkey == selection.production_hotkey), None)
        if verdict and (not verdict.cleared or verdict.mean_gain != selection.winner_gain):
            raise ValueError("selected winner does not match the cleared final verdict")
        finalist = next((row for row in finalists if row.hotkey == selection.production_hotkey), None)
        if finalist:
            production = finalist
        else:
            if (baseline[0]["role"] != "incumbent"
                or selection.production_hotkey != round_record["incumbent_hotkey"]
                or not production_team_id or selection.winner_gain != 0):
                raise ValueError("production hotkey is not a finalist or frozen incumbent")
            production = Payee(
                selection.production_hotkey, production_team_id,
                qualified_by_hotkey.get(selection.production_hotkey, False),
            )
    plan = weekly_weight_plan(
        operator_hotkey=operator_hotkey, production=production,
        finalists=finalists, uid_by_hotkey=uid_by_hotkey,
        burn_uid=burn_uid, winner_gain=selection.winner_gain,
    )
    judge_diagnostics = final_judge_diagnostics(
        sample=backend.list_sample(round_id), pair_rows=list(selection.final_pair_rows),
        stage=stages[-1]["stage"], finalist_hotkeys=finalist_hotkeys,
    )
    results = {
        "decision": selection.decision,
        "production_hotkey": selection.production_hotkey,
        "fallback_reference_hotkey": baseline[0]["hotkey"]
        if selection.decision == "reference_fallback" else None,
        "winner_hotkey": selection.production_hotkey if selection.decision == "new_crown" else None,
        "winner_gain": selection.winner_gain,
        "finalists": [asdict(row) for row in selection.verdicts],
        "gold": {hotkey: asdict(result) for hotkey, result in sorted(gold_by_hotkey.items())},
        "gold_check": dict(gold_check) if gold_check else None,
        "known_answer_audit": dict(known_answer_audit) if known_answer_audit else None,
        "final_pair_scores": list(selection.final_pair_rows),
        "judge_diagnostics": judge_diagnostics,
        "copy_audit": {hotkey: dict(row) for hotkey, row in sorted(copy_audit.items())},
        "code_audit": {"evidence_sha256": digest,
                       "failed_checks": dict(sorted(failed_checks.items()))},
        "worker_security_audit": {hotkey: dict(row) for hotkey, row in sorted(worker_security_audit.items())},
        "worker_inference_cost": backend.worker_inference_cost_summary(round_id),
    }
    weights = {
        "vector": [{"uid": uid, "weight": weight} for uid, weight in plan.weights_by_uid.items()],
        "roles": [{"hotkey": hotkey, "role": role, "share": share}
                  for hotkey, role, share in plan.roles],
        "burned_fraction": plan.burned_fraction,
    }
    bundle = publication_bundle(round_record, results=results, weights=weights)
    receipt = sign_publication(bundle, signer)
    backend.publish_round(round_id, receipt)
    return receipt
