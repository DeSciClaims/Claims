"""Turn an audited final selection into the signed public weekly result."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, Protocol

from .final_selection import FinalSelection
from .gold import GoldResult
from .publication import publication_bundle, sign_publication
from .weights import Payee, weekly_weight_plan


class PublicationBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def publish_round(self, round_id: str, signed_receipt: dict[str, Any]) -> dict[str, Any]: ...


def publish_selected_round(
    round_id: str, *, backend: PublicationBackend, signer: Any,
    selection: FinalSelection, operator_hotkey: str,
    uid_by_hotkey: Mapping[str, int], qualified_by_hotkey: Mapping[str, bool],
    production_team_id: str | None,
    gold_by_hotkey: Mapping[str, GoldResult],
    burn_uid: int = 0,
) -> dict[str, Any]:
    """Sign results and 70/20/10 shares; backend checks frozen inputs and grid."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold_closed"}:
        raise ValueError("round is not ready for final publication")
    finalist_hotkeys = {row.hotkey for row in selection.verdicts}
    if not finalist_hotkeys <= set(qualified_by_hotkey):
        raise ValueError("finalist qualification snapshot is incomplete")
    stages = backend.list_stages(round_id)
    if not stages or stages[-1]["stage"] not in {"final", "week_one"}:
        raise ValueError("final stage is missing")
    if {row["hotkey"] for row in stages[-1]["entries"] if row["role"] == "challenger"} != finalist_hotkeys:
        raise ValueError("publication finalists differ from frozen final stage")
    baseline = [row for row in stages[-1]["entries"] if row["role"] in {"incumbent", "reference"}]
    if (len(baseline) != 1 or
        (round_record["incumbent_hotkey"] is not None
         and baseline[0]["hotkey"] != round_record["incumbent_hotkey"])):
        raise ValueError("frozen production baseline is missing")
    if round_record["incumbent_hotkey"] is not None and production_team_id != baseline[0]["team_id"]:
        raise ValueError("production team differs from frozen baseline")
    finalists = [Payee(
        row.hotkey, row.team_id, qualified_by_hotkey[row.hotkey], row.mean_gain,
        copy=row.reason == "copy", code_passed=row.reason != "code_failed",
    ) for row in selection.verdicts]
    if set(gold_by_hotkey) != finalist_hotkeys and round_record["status"] == "gold_closed":
        raise ValueError("closed Gold results must cover every finalist")
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
            if (selection.production_hotkey != round_record["incumbent_hotkey"]
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
    results = {
        "decision": selection.decision,
        "production_hotkey": selection.production_hotkey,
        "winner_hotkey": selection.production_hotkey if selection.decision == "new_crown" else None,
        "winner_gain": selection.winner_gain,
        "finalists": [asdict(row) for row in selection.verdicts],
        "gold": {hotkey: asdict(result) for hotkey, result in sorted(gold_by_hotkey.items())},
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
