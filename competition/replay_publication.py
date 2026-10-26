"""Canonical signed receipt for an independent, locally scored weekly vector."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Callable, Mapping

from .final_selection import FinalSelection
from .final_stage import effective_final_stage
from .gold import GoldResult
from .publication import canonical_sha256
from .weights import WeightPlan


REPLAY_PUBLICATION_SCHEMA = "claims_competition_independent_replay_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def replay_publication_bundle(
    round_id: str, *, backend: Any, selection: FinalSelection,
    weight_plan: WeightPlan, gold_policy_id: str,
    gold_evidence_sha256: str, qualification_evidence_sha256: str,
    gold_by_hotkey: Mapping[str, GoldResult],
    case_decisions_sha256: str,
    allow_gold_adjustments: bool = False,
) -> dict[str, Any]:
    """Bind independent scores and audited inputs to an explicit Gold policy.

    The caller must separately establish the Gold outcome, eligibility, and
    current UID mapping used to calculate ``selection`` and ``weight_plan``.
    """
    snapshot = backend.snapshot
    if snapshot.get("round_id") != round_id or not gold_policy_id or len(gold_policy_id) > 120:
        raise ValueError("replay publication has no frozen round or Gold policy")
    if any(not isinstance(value, str) or not _SHA256.fullmatch(value)
           for value in (gold_evidence_sha256, qualification_evidence_sha256,
                         case_decisions_sha256)):
        raise ValueError("replay publication lacks Gold or qualification evidence")
    if (set(gold_by_hotkey) != {row.hotkey for row in selection.verdicts}
        or any(not isinstance(row, GoldResult) for row in gold_by_hotkey.values())):
        raise ValueError("replay publication lacks finalist Gold results")
    stage = effective_final_stage(backend.list_stages(round_id))
    local_pairs = backend.list_stage_pairs(round_id, stage)
    adjusted_pairs = selection.final_pair_rows
    if (not local_pairs or len(local_pairs) != len(adjusted_pairs)
        or (not allow_gold_adjustments and tuple(local_pairs) != adjusted_pairs)
        or any({key: value for key, value in raw.items()
                if key not in {"challenger_score", "baseline_score"}}
               != {key: value for key, value in adjusted.items()
                   if key not in {"challenger_score", "baseline_score"}}
               for raw, adjusted in zip(local_pairs, adjusted_pairs, strict=True))):
        raise ValueError("replay publication differs from local final score grid")
    audit = backend.get_final_audit(round_id)
    if (not isinstance(audit, dict)
        or not _SHA256.fullmatch(str(audit.get("input_sha256", "")))
        or not isinstance(audit.get("result_private"), dict)
        or audit["result_private"].get("round_id") != round_id
        or audit["result_private"].get("stage") != stage):
        raise ValueError("replay publication lacks a frozen local final audit")
    frozen = (
        "network", "rules_sha256", "configuration_sha256", "archive_block_hash",
        "drand_round", "drand_seed", "paper_pool_sha256",
        "duplicate_review_sha256", "baseline_package_sha256",
        "reference_package_sha256",
    )
    if any(snapshot.get(key) is None for key in frozen):
        raise ValueError("replay publication lacks frozen input identity")
    weights = {
        "vector": [{"uid": uid, "weight": weight}
                   for uid, weight in sorted(weight_plan.weights_by_uid.items())],
        "roles": [{"hotkey": hotkey, "role": role, "share": share}
                  for hotkey, role, share in weight_plan.roles],
        "burned_fraction": weight_plan.burned_fraction,
    }
    return {
        "schema_version": REPLAY_PUBLICATION_SCHEMA,
        "round_id": round_id,
        **{key: snapshot[key] for key in frozen},
        "local_stage": stage,
        "local_sample_sha256": canonical_sha256(backend.list_sample(round_id)),
        "local_pairs_sha256": canonical_sha256(local_pairs),
        "gold_adjusted_pairs_sha256": canonical_sha256(adjusted_pairs),
        "local_final_audit_input_sha256": audit["input_sha256"],
        "local_final_audit_sha256": canonical_sha256(audit["result_private"]),
        "gold_policy_id": gold_policy_id,
        "gold_evidence_sha256": gold_evidence_sha256,
        "qualification_evidence_sha256": qualification_evidence_sha256,
        "results": {
            "decision": selection.decision,
            "production_hotkey": selection.production_hotkey,
            "winner_gain": selection.winner_gain,
            "finalists": [asdict(row) for row in selection.verdicts],
            "gold": {hotkey: asdict(row)
                     for hotkey, row in sorted(gold_by_hotkey.items())},
            "gold_case_decisions_sha256": case_decisions_sha256,
        },
        "weights": weights,
    }


def sign_replay_publication(bundle: Mapping[str, Any], signer: Any) -> dict[str, Any]:
    if bundle.get("schema_version") != REPLAY_PUBLICATION_SCHEMA:
        raise ValueError("replay publication schema is invalid")
    digest = canonical_sha256(bundle)
    signature = signer.sign(bytes.fromhex(digest))
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("replay publication signer returned an invalid signature")
    return {
        "bundle": dict(bundle), "bundle_sha256": digest,
        "signer_hotkey": signer.ss58_address, "signature_hex": signature.hex(),
    }


def verify_replay_publication(
    receipt: Mapping[str, Any], *,
    verify_signature: Callable[[str, bytes, bytes], bool],
) -> dict[str, Any]:
    if (set(receipt) != {"bundle", "bundle_sha256", "signer_hotkey", "signature_hex"}
        or not isinstance(receipt["bundle"], dict)
        or receipt["bundle"].get("schema_version") != REPLAY_PUBLICATION_SCHEMA
        or not isinstance(receipt["signer_hotkey"], str)
        or not receipt["signer_hotkey"]
        or receipt["bundle_sha256"] != canonical_sha256(receipt["bundle"])):
        raise ValueError("replay publication receipt differs from signed bundle")
    try:
        signature = bytes.fromhex(receipt["signature_hex"])
    except (TypeError, ValueError) as exc:
        raise ValueError("replay publication signature is malformed") from exc
    if (len(signature) != 64
        or not verify_signature(receipt["signer_hotkey"],
                                bytes.fromhex(receipt["bundle_sha256"]), signature)):
        raise ValueError("replay publication signature is invalid")
    return receipt["bundle"]
