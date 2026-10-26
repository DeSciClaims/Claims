"""Verify and tally votes for a second validator's separate Gold review."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Callable, Mapping, Sequence

from .gold import GoldCase, GoldVote, decide_gold_case, gold_result
from .publication import canonical_sha256


def replay_gold_vote_message(
    round_id: str, case_id: str, voter_hotkey: str, choice: str,
) -> bytes:
    payload = {
        "schema_version": "claims_replay_gold_vote_v1",
        "round_id": round_id, "case_id": case_id,
        "voter_hotkey": voter_hotkey, "choice": choice,
    }
    return b"claims-replay-gold-vote-v1\0" + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()


def _verify_signature(hotkey: str, message: bytes, signature: bytes) -> bool:
    from bittensor_wallet import Keypair

    return bool(Keypair(ss58_address=hotkey).verify(message, signature))


def tally_replay_gold_review(
    review: Mapping[str, Any], *,
    voters: Sequence[Mapping[str, str]], votes: Sequence[Mapping[str, str]],
    verify_signature: Callable[[str, bytes, bytes], bool] = _verify_signature,
) -> dict[str, Any]:
    """Return a deterministic tally, rejecting primary or changed signatures."""
    if (review.get("schema_version") != "claims_replay_gold_review_v1"
        or not isinstance(review.get("round_id"), str)
        or not isinstance(review.get("cases"), list)):
        raise ValueError("replay Gold review is malformed")
    round_id = review["round_id"]
    declared_finalists = review.get("finalist_hotkeys")
    if (not isinstance(declared_finalists, list) or not declared_finalists
        or any(not isinstance(hotkey, str) or not hotkey
               for hotkey in declared_finalists)
        or len(set(declared_finalists)) != len(declared_finalists)):
        raise ValueError("replay Gold finalists are malformed")
    cases = {}
    finalist_hotkeys = set(declared_finalists)
    for row in review["cases"]:
        if (not isinstance(row, dict)
            or not isinstance(row.get("case_id"), str)
            or not row["case_id"].startswith("replay_gold_")
            or row["case_id"] in cases
            or not isinstance(row.get("excluded_team_ids"), list)
            or not isinstance(row.get("review_purposes"), list)
            or not set(row["review_purposes"]) <= {"check", "escalation", "challenge"}
            or not row["review_purposes"]):
            raise ValueError("replay Gold case is malformed")
        cases[row["case_id"]] = row
        if row.get("challenger_hotkey") not in finalist_hotkeys:
            raise ValueError("replay Gold case is outside local finalists")
    if not cases:
        raise ValueError("replay Gold has no cases")
    teams = {}
    for row in voters:
        if (not isinstance(row, Mapping)
            or not isinstance(row.get("voter_hotkey"), str)
            or not row["voter_hotkey"]
            or not isinstance(row.get("team_id"), str)
            or not row["team_id"]
            or row["voter_hotkey"] in teams):
            raise ValueError("replay Gold voter set is malformed")
        teams[row["voter_hotkey"]] = row["team_id"]
    by_case: dict[str, list[GoldVote]] = {case_id: [] for case_id in cases}
    seen = set()
    for vote in votes:
        if not isinstance(vote, Mapping):
            raise ValueError("replay Gold vote is malformed")
        case_id = vote.get("case_id")
        hotkey = vote.get("voter_hotkey")
        choice = vote.get("choice")
        if (case_id not in cases or hotkey not in teams
            or (case_id, hotkey) in seen
            or choice not in {"challenger", "incumbent", "neither"}
            or teams[hotkey] in cases[case_id]["excluded_team_ids"]):
            raise ValueError("replay Gold vote is outside its qualified case")
        seen.add((case_id, hotkey))
        message = replay_gold_vote_message(round_id, case_id, hotkey, choice)
        try:
            signature = bytes.fromhex(vote["signature_hex"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("replay Gold vote signature is malformed") from exc
        if (len(signature) != 64
            or vote.get("signed_payload_sha256") != hashlib.sha256(message).hexdigest()
            or not verify_signature(hotkey, message, signature)):
            raise ValueError("replay Gold vote signature is invalid")
        by_case[case_id].append(GoldVote(hotkey, choice))
    decisions = {}
    checks = {hotkey: [] for hotkey in finalist_hotkeys}
    challenge_accuracy = {hotkey: {"correct": 0, "total": 0} for hotkey in teams}
    for case_id, row in sorted(cases.items()):
        case = GoldCase(
            case_id, row["paper_id"], row["challenger_hotkey"],
            row["incumbent_hotkey"], frozenset(row["excluded_team_ids"]),
        )
        decision = decide_gold_case(
            case, by_case[case_id], qualified_team_by_hotkey=teams,
        )
        decisions[case_id] = asdict(decision)
        if "check" in row["review_purposes"]:
            checks[case.challenger_hotkey].append(decision)
        if "challenge" in row["review_purposes"]:
            answer = row.get("answer_private") or {}
            if (answer.get("kind") != "known_answer"
                or answer.get("correct_choice") not in {"challenger", "incumbent"}):
                raise ValueError("replay Gold challenge answer is invalid")
            for vote in by_case[case_id]:
                score = challenge_accuracy[vote.voter_hotkey]
                score["total"] += 1
                score["correct"] += vote.choice == answer["correct_choice"]
    evidence = {
        "round_id": round_id,
        "review_sha256": canonical_sha256(dict(review)),
        "voters_sha256": canonical_sha256(sorted(
            [dict(row) for row in voters], key=lambda row: row["voter_hotkey"],
        )),
        "votes_sha256": canonical_sha256(sorted(
            [dict(row) for row in votes],
            key=lambda row: (row["case_id"], row["voter_hotkey"]),
        )),
        "case_decisions": decisions,
        "gold_by_hotkey": {hotkey: asdict(gold_result(rows))
                           for hotkey, rows in sorted(checks.items())},
        "challenge_accuracy": challenge_accuracy,
    }
    return {**evidence, "evidence_sha256": canonical_sha256(evidence)}
