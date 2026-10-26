"""Draw a second validator's Gold cases from its own frozen Silver receipts."""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from .final_stage import effective_final_stage
from .gold import draw_gold_cases
from .gold_challenges import build_known_answer_challenges
from .gold_controller import ContestedClaim, frozen_gold_candidates
from .publication import canonical_sha256


def _identity(row: ContestedClaim) -> dict[str, Any]:
    return {
        "case": {
            "case_id": row.case.case_id,
            "paper_id": row.case.paper_id,
            "challenger_hotkey": row.case.challenger_hotkey,
            "incumbent_hotkey": row.case.incumbent_hotkey,
            "excluded_team_ids": sorted(row.case.excluded_team_ids),
        },
        "case_public": dict(row.case_public),
        "answer_private": dict(row.answer_private),
    }


def replay_known_answer_challenges(
    round_id: str, *, backend: Any, certified: list[dict[str, Any]],
    limit: int = 40,
) -> list[ContestedClaim]:
    """Select exact-PDF certified cases with this validator's own draw domain."""
    stage = effective_final_stage(backend.list_stages(round_id))
    seed_hex = backend.snapshot.get("drand_seed")
    if backend.snapshot.get("round_id") != round_id or not isinstance(seed_hex, str):
        raise ValueError("replay Gold round or seed is invalid")
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise ValueError("replay Gold seed is malformed") from exc
    if len(seed) != 32:
        raise ValueError("replay Gold seed is malformed")
    return build_known_answer_challenges(
        round_id, certified=certified,
        final_sample=[row for row in backend.list_sample(round_id)
                      if row["stage"] == stage],
        final_entries=backend.list_stages(round_id)[-1]["entries"],
        seed=hashlib.sha256(b"claims-replay-gold-known-v1\0" + seed).digest(),
        limit=limit,
    )


def prepare_replay_gold_review(
    round_id: str, *, backend: Any,
    known_answers: Sequence[ContestedClaim],
    check_limit: int = 300, escalation_limit: int = 1700,
    minimum_known_answers: int = 40,
) -> dict[str, Any]:
    """Freeze a new review, never transferring primary Gold votes or verdicts.

    The post-deadline verified beacon draws cases from the immutable local
    candidate set. A separate case namespace makes the miner's signed vote
    unambiguously belong to this validator's review.
    """
    snapshot = backend.snapshot
    if snapshot.get("round_id") != round_id:
        raise ValueError("replay Gold round differs from verified snapshot")
    stage = effective_final_stage(backend.list_stages(round_id))
    if backend.get_final_audit(round_id) is None:
        raise ValueError("replay Gold needs a frozen local final audit")
    seed_hex = snapshot.get("drand_seed")
    if not isinstance(seed_hex, str):
        raise ValueError("replay Gold needs a verified draw seed")
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise ValueError("replay Gold seed is malformed") from exc
    if len(seed) != 32 or check_limit < 1 or escalation_limit < 0:
        raise ValueError("replay Gold draw settings are invalid")
    candidates = frozen_gold_candidates(round_id, backend=backend)
    known = list(known_answers)
    if minimum_known_answers < 0 or len(known) < minimum_known_answers:
        raise ValueError("replay Gold lacks certified known-answer cases")
    source_ids = [row.case.case_id for row in [*candidates, *known]]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("replay Gold source case IDs collide")
    final_entries = backend.list_stages(round_id)[-1]["entries"]
    finalists = {row["hotkey"] for row in final_entries if row["role"] == "challenger"}
    baselines = {row["hotkey"] for row in final_entries
                 if row["role"] in {"incumbent", "reference"}}
    if (not finalists or len(baselines) != 1
        or any(row.case.challenger_hotkey not in finalists
               or row.case.incumbent_hotkey not in baselines for row in candidates)):
        raise ValueError("replay Gold cases differ from local finalists")
    for row in known:
        answer = row.answer_private
        if (row.case.challenger_hotkey not in finalists
            or row.case.incumbent_hotkey not in baselines
            or not row.case.excluded_team_ids
            or set(row.case_public) != {
                "question", "statement", "source_quotes", "source_span_ids",
                "claim_side", "challenger_hotkey", "incumbent_hotkey",
            }
            or answer.get("kind") != "known_answer"
            or answer.get("correct_choice") not in {"challenger", "incumbent"}
            or not answer.get("source_challenge_id")
            or len(str(answer.get("certificate_sha256") or "")) != 64):
            raise ValueError("replay Gold challenge lacks a certified final contract")
    check_seed = hashlib.sha256(b"claims-replay-gold-check-v1\0" + seed).digest()
    escalation_seed = hashlib.sha256(b"claims-replay-gold-escalation-v1\0" + seed).digest()
    check = draw_gold_cases([row.case for row in candidates], seed=check_seed,
                            limit=check_limit)
    escalations = draw_gold_cases(
        [row.case for row in candidates if row.answer_private.get("primary_split") is True],
        seed=escalation_seed, limit=max(1, escalation_limit),
    ) if escalation_limit else ()
    checks = {row.case_id for row in check}
    escalated = {row.case_id for row in escalations}
    selected = [row for row in candidates if row.case.case_id in checks | escalated]
    selected.extend(known)
    rows = []
    for row in selected:
        source_id = row.case.case_id
        case_id = "replay_gold_" + hashlib.sha256(
            b"claims-replay-gold-case-v1\0" + round_id.encode() + b"\0"
            + source_id.encode(),
        ).hexdigest()[:32]
        purposes = [purpose for purpose, present in (
            ("check", source_id in checks),
            ("escalation", source_id in escalated),
            ("challenge", row in known),
        ) if present]
        rows.append({
            "case_id": case_id, "source_case_id": source_id,
            "paper_id": row.case.paper_id,
            "challenger_hotkey": row.case.challenger_hotkey,
            "incumbent_hotkey": row.case.incumbent_hotkey,
            "excluded_team_ids": sorted(row.case.excluded_team_ids),
            "case_public": dict(row.case_public),
            "answer_private": dict(row.answer_private),
            "review_purposes": purposes,
        })
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("replay Gold case namespace collides")
    candidate_identity = [_identity(row) for row in candidates]
    review = {
        "schema_version": "claims_replay_gold_review_v1",
        "round_id": round_id, "stage": stage,
        "drand_seed": seed_hex,
        "local_candidate_sha256": canonical_sha256(candidate_identity),
        "finalist_hotkeys": sorted(finalists),
        "known_answer_sha256": canonical_sha256([_identity(row) for row in known]),
        "check_count": len(check), "escalation_count": len(escalations),
        "known_answer_count": len(known),
        "cases": rows,
    }
    return backend.freeze_gold_review(round_id, review)
