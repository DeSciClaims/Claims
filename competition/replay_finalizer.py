"""Finish a second validator's closed Gold review with its own signed vector."""

from __future__ import annotations

from typing import Any, Callable

from .gold import GoldResult, VoteDecision
from .review_policy import uses_miner_review
from .publication import canonical_sha256
from .replay_final_selection import select_replay_final
from .replay_publication import (
    replay_publication_bundle, sign_replay_publication, verify_replay_publication,
)
from .replay_weights import replay_weight_plan


def finalize_replay(
    round_id: str, *, backend: Any, tally: dict[str, Any],
    signer: Any, publication_client: Any, subtensor: Any, netuid: int,
    owner_hotkey: str, operator_hotkey: str,
    verify_signature: Callable[[str, bytes, bytes], bool],
) -> dict[str, Any]:
    """Publish only after the primary result and this validator's Gold close."""
    if not publication_client.primary_published(round_id):
        return {"status": "waiting_for_primary_publication"}
    prior = backend.get_replay_publication(round_id)
    if prior is not None:
        bundle = verify_replay_publication(prior, verify_signature=verify_signature)
        if (bundle.get("round_id") != round_id
            or prior["signer_hotkey"] != signer.ss58_address
            or bundle.get("gold_evidence_sha256") != tally.get("evidence_sha256")):
            raise ValueError("frozen replay publication differs from closed Gold")
        publication_client.publish(round_id, prior, verify_signature=verify_signature)
        return {"status": "independent_published",
                "bundle_sha256": prior["bundle_sha256"]}
    reviewed = uses_miner_review(backend.snapshot)
    review = backend.get_gold_review(round_id) if reviewed else None
    if reviewed and (not isinstance(review, dict) or review.get("round_id") != round_id
        or tally.get("review_sha256") != canonical_sha256(review)):
        raise ValueError("replay final lacks its own closed Gold review")
    try:
        gold = {hotkey: GoldResult(**row)
                for hotkey, row in tally["gold_by_hotkey"].items()}
        decisions = {case_id: VoteDecision(**row)
                     for case_id, row in tally["case_decisions"].items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("closed replay Gold tally is malformed") from exc
    selection = select_replay_final(
        round_id, backend=backend, gold_by_hotkey=gold,
        gold_review=review, case_decisions=decisions,
    )
    entries = backend.list_stages(round_id)[-1]["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("replay final baseline is missing")
    payout_hotkeys = sorted({row.hotkey for row in selection.verdicts}
                            | ({baseline[0]["hotkey"]}
                               if baseline[0]["role"] == "incumbent" else set()))
    cutoff = backend.snapshot["rules_public"]["deadline_at"]
    qualification_rows = []
    windows = set()
    for offset in range(0, len(payout_hotkeys), 8):
        evidence = backend.inputs.qualification_evidence(
            round_id, hotkeys=payout_hotkeys[offset:offset + 8],
            deadline_at=cutoff,
        )
        windows.add((evidence["window_start"], evidence["cutoff"]))
        qualification_rows.extend(evidence["hotkeys"])
    if len(windows) != 1 or len(qualification_rows) != len(payout_hotkeys):
        raise ValueError("replay qualification windows differ")
    qualified = {row["hotkey"]: row["qualified"] for row in qualification_rows}
    metagraph = subtensor.metagraph(netuid=netuid)
    hotkeys = list(metagraph.hotkeys)
    if (not hotkeys or hotkeys[0] != owner_hotkey
        or operator_hotkey not in hotkeys or len(set(hotkeys)) != len(hotkeys)):
        raise ValueError("current replay metagraph owner or operator is invalid")
    weights = replay_weight_plan(
        selection, operator_hotkey=operator_hotkey,
        incumbent_hotkey=baseline[0]["hotkey"]
        if baseline[0]["role"] == "incumbent" else None,
        incumbent_team_id=baseline[0]["team_id"]
        if baseline[0]["role"] == "incumbent" else None,
        qualified_by_hotkey=qualified,
        uid_by_hotkey={hotkey: uid for uid, hotkey in enumerate(hotkeys)},
    )
    qualification = {
        "round_id": round_id,
        "window_start": next(iter(windows))[0],
        "cutoff": next(iter(windows))[1],
        "hotkeys": sorted(qualification_rows, key=lambda row: row["hotkey"]),
    }
    bundle = replay_publication_bundle(
        round_id, backend=backend, selection=selection, weight_plan=weights,
        gold_policy_id="independent-gold-v1" if reviewed else "silver-tiebreak-v1",
        gold_evidence_sha256=tally["evidence_sha256"],
        qualification_evidence_sha256=canonical_sha256(qualification),
        gold_by_hotkey=gold,
        case_decisions_sha256=canonical_sha256(tally["case_decisions"]),
        allow_gold_adjustments=reviewed,
    )
    receipt = sign_replay_publication(bundle, signer)
    backend.freeze_replay_publication(
        round_id, receipt, expected_signer_hotkey=signer.ss58_address,
        verify_signature=verify_signature,
    )
    publication_client.publish(round_id, receipt, verify_signature=verify_signature)
    return {"status": "independent_published",
            "bundle_sha256": receipt["bundle_sha256"]}
