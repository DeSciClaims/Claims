"""Deterministic Gold sampling and qualified miner-vote decisions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Mapping

from .policy import gold_veto


Choice = Literal["challenger", "incumbent", "neither"]


@dataclass(frozen=True)
class GoldCase:
    case_id: str
    paper_id: str
    challenger_hotkey: str
    incumbent_hotkey: str
    excluded_team_ids: frozenset[str]


@dataclass(frozen=True)
class GoldVote:
    voter_hotkey: str
    choice: Choice


@dataclass(frozen=True)
class VoteDecision:
    choice: Choice | None
    qualified_vote_count: int
    leading_vote_count: int


@dataclass(frozen=True)
class GoldResult:
    sampled_cases: int
    decided_contested_claims: int
    incumbent_supported: int
    incumbent_share: float | None
    refusal: bool


def draw_gold_cases(cases: list[GoldCase], *, seed: bytes, limit: int = 300) -> tuple[GoldCase, ...]:
    """Draw without replacement from contested final claims after the sample seed."""
    if len(seed) != 32 or limit < 1:
        raise ValueError("Gold draw needs a 32-byte seed and positive limit")
    if len({case.case_id for case in cases}) != len(cases) or any(
        not case.case_id or not case.paper_id or not case.challenger_hotkey
        or not case.incumbent_hotkey or not case.excluded_team_ids
        for case in cases
    ):
        raise ValueError("Gold cases need unique complete identities")
    ranked = sorted(cases, key=lambda case: (
        hashlib.sha256(b"claims-gold-case-v1\0" + seed + b"\0" + case.case_id.encode()).digest(),
        case.case_id,
    ))
    return tuple(ranked[:limit])


def decide_gold_case(
    case: GoldCase, votes: list[GoldVote], *,
    qualified_team_by_hotkey: Mapping[str, str], quorum: int = 11,
) -> VoteDecision:
    """At least 11 eligible voters, with 75% agreement, decide a claim."""
    if quorum < 1:
        raise ValueError("Gold quorum must be positive")
    if len({vote.voter_hotkey for vote in votes}) != len(votes):
        raise ValueError("one voter cannot vote twice on a Gold case")
    counts: dict[Choice, int] = {"challenger": 0, "incumbent": 0, "neither": 0}
    qualified_count = 0
    for vote in votes:
        if vote.choice not in counts:
            raise ValueError("unknown Gold vote choice")
        team = qualified_team_by_hotkey.get(vote.voter_hotkey)
        if team is None or team in case.excluded_team_ids:
            continue
        qualified_count += 1
        counts[vote.choice] += 1
    leading = max(counts.values())
    decided = None
    if qualified_count >= quorum and 4 * leading >= 3 * qualified_count:
        decided = next(choice for choice, count in counts.items() if count == leading)
    return VoteDecision(decided, qualified_count, leading)


def gold_result(decisions: list[VoteDecision]) -> GoldResult:
    contested = [decision.choice for decision in decisions
                 if decision.choice in {"challenger", "incumbent"}]
    incumbent = contested.count("incumbent")
    total = len(contested)
    return GoldResult(
        sampled_cases=len(decisions), decided_contested_claims=total,
        incumbent_supported=incumbent,
        incumbent_share=incumbent / total if total else None,
        refusal=gold_veto(decided_claims=total, incumbent_supported=incumbent),
    )


def escalated_choice(*, tiebreak: Choice, vote_decision: VoteDecision) -> Choice:
    """A decided eligible miner vote overrides the frozen tiebreak judge."""
    return vote_decision.choice if vote_decision.choice is not None else tiebreak
