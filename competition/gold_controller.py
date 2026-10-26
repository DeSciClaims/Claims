"""Trusted Gold review preparation and tally from durable backend records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .gold import GoldCase, GoldResult, GoldVote, decide_gold_case, draw_gold_cases, gold_result


class GoldBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def freeze_gold_voters(self, round_id: str, voters: list[dict[str, str]]) -> dict[str, Any]: ...
    def create_gold_cases(self, round_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]: ...
    def list_gold_cases(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_voters(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_votes(self, round_id: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ContestedClaim:
    """A frozen primary-judge split, prepared by the final evaluator."""

    case: GoldCase
    case_public: Mapping[str, Any]
    answer_private: Mapping[str, Any]


def prepare_gold_review(
    round_id: str, *, backend: GoldBackend, candidates: list[ContestedClaim],
    voters: list[dict[str, str]], limit: int = 300,
) -> dict[str, Any]:
    """Use the released drand seed and seal at most 300 contested claims."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold"}:
        raise ValueError("final evaluation has not reached Gold review")
    seed = bytes.fromhex(round_record["drand_seed"])
    if len(seed) != 32:
        raise ValueError("round has no valid released draw seed")
    if len({candidate.case.case_id for candidate in candidates}) != len(candidates):
        raise ValueError("contested Gold case IDs must be distinct")
    by_id = {candidate.case.case_id: candidate for candidate in candidates}
    gold_seed = hashlib.sha256(b"claims-gold-draw-v1\0" + seed).digest()
    selected = draw_gold_cases([candidate.case for candidate in candidates], seed=gold_seed, limit=limit)
    if not selected:
        raise ValueError("Gold review needs at least one contested claim")
    backend.freeze_gold_voters(round_id, voters)
    receipt = backend.create_gold_cases(round_id, [{
        "case_id": case.case_id, "paper_id": case.paper_id,
        "challenger_hotkey": case.challenger_hotkey,
        "incumbent_hotkey": case.incumbent_hotkey,
        "excluded_team_ids": sorted(case.excluded_team_ids),
        "case_public": dict(by_id[case.case_id].case_public),
        "answer_private": dict(by_id[case.case_id].answer_private),
    } for case in selected])
    return {"round_id": round_id, "case_count": len(selected), "receipt": receipt}


def tally_closed_gold_review(
    round_id: str, *, backend: GoldBackend,
) -> dict[str, GoldResult]:
    """Tally only after closing intake; group the veto by challenger."""
    if backend.get_round(round_id)["status"] != "gold_closed":
        raise ValueError("Gold voting must be closed before tally")
    cases = backend.list_gold_cases(round_id)
    voters = backend.list_gold_voters(round_id)
    votes = backend.list_gold_votes(round_id)
    team_by_hotkey = {row["voter_hotkey"]: row["team_id"] for row in voters}
    if len(team_by_hotkey) != len(voters):
        raise ValueError("Gold voter snapshot contains duplicate hotkeys")
    cases_by_id = {row["case_id"]: row for row in cases}
    if len(cases_by_id) != len(cases) or any(row["round_id"] != round_id for row in cases):
        raise ValueError("Gold case ledger differs from closed round")
    votes_by_case: dict[str, list[GoldVote]] = {case_id: [] for case_id in cases_by_id}
    for row in votes:
        if row["case_id"] not in cases_by_id:
            raise ValueError("Gold vote belongs to another case")
        votes_by_case[row["case_id"]].append(GoldVote(row["voter_hotkey"], row["choice"]))
    decisions_by_challenger: dict[str, list] = {}
    for row in cases:
        case = GoldCase(
            row["case_id"], row["paper_id"], row["challenger_hotkey"],
            row["incumbent_hotkey"], frozenset(row["excluded_team_ids"]),
        )
        decision = decide_gold_case(case, votes_by_case[case.case_id],
                                    qualified_team_by_hotkey=team_by_hotkey)
        decisions_by_challenger.setdefault(case.challenger_hotkey, []).append(decision)
    return {hotkey: gold_result(decisions) for hotkey, decisions in decisions_by_challenger.items()}
