"""Small, auditable policy primitives for the weekly pipeline competition.

The paper draw is independent of input ordering and Python's random module.
Inputs and seed are frozen before the draw; the same inputs produce identical
stage membership and judge assignments on every validator.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from statistics import mean, stdev


@dataclass(frozen=True)
class Draw:
    prescreen: tuple[str, ...]
    screen: tuple[str, ...]
    final: tuple[str, ...]
    judges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PaperPairScore:
    paper_id: str
    challenger_score: float | None
    incumbent_score: float | None
    infrastructure_fault: bool = False


@dataclass(frozen=True)
class PairedResult:
    paper_count: int
    mean_gain: float
    standard_error: float
    gains: tuple[float, ...]


def commitment_hash(package: bytes, hotkey_public_key: bytes, salt: bytes) -> str:
    """Hash the exact package bytes, decoded hotkey public key, and 32-byte salt.

    This is the ``h`` in the proposal's on-chain ``Sha256(h)`` commitment. The
    caller is responsible for the chain's outer hash and manifest identifier.
    """
    if not hotkey_public_key:
        raise ValueError("hotkey public key is required")
    if len(salt) != 32:
        raise ValueError("salt must be exactly 32 bytes")
    return hashlib.sha256(package + hotkey_public_key + salt).hexdigest()


def draw_papers(
    paper_ids: list[str] | tuple[str, ...],
    *,
    seed: bytes,
    prescreen_count: int,
    screen_count: int,
    final_count: int,
    judge_ids: tuple[str, str] = ("J1", "J2"),
) -> Draw:
    """Draw disjoint stages without replacement from a frozen eligible pool."""
    if len(seed) < 16:
        raise ValueError("post-deadline drand seed must be at least 16 bytes")
    counts = (prescreen_count, screen_count, final_count)
    if any(count < 0 for count in counts):
        raise ValueError("stage counts must be nonnegative")
    if len(judge_ids) != 2 or judge_ids[0] == judge_ids[1]:
        raise ValueError("two distinct judge identifiers are required")
    pool = set(paper_ids)
    if len(pool) != len(paper_ids) or not pool or any(not value for value in pool):
        raise ValueError("paper pool must contain distinct nonempty identifiers")
    if sum(counts) > len(pool):
        raise ValueError("requested sample exceeds eligible pool")
    ranked = sorted(
        pool,
        key=lambda paper_id: (
            hashlib.sha256(b"claims-weekly-paper-v1\0" + seed + b"\0" + paper_id.encode()).digest(),
            paper_id,
        ),
    )
    a, b, c = counts
    prescreen = tuple(ranked[:a])
    screen = tuple(ranked[a : a + b])
    final = tuple(ranked[a + b : a + b + c])
    first_judge = hashlib.sha256(b"claims-weekly-judge-v1\0" + seed).digest()[0] & 1
    judges = tuple((paper_id, judge_ids[(first_judge + index) % 2]) for index, paper_id in enumerate(final))
    return Draw(prescreen, screen, final, judges)


def paired_result(papers: list[PaperPairScore] | tuple[PaperPairScore, ...]) -> PairedResult:
    """Aggregate same-paper, same-pass scores; infrastructure faults leave both sides out.

    A missing package output is scored zero. If the incumbent fails due to a
    package fault, it too scores zero; the caller separately applies the
    proposal's >20% incumbent-failure fallback rule.
    """
    seen: set[str] = set()
    gains: list[float] = []
    for paper in papers:
        if not paper.paper_id or paper.paper_id in seen:
            raise ValueError("paper scores require unique nonempty paper IDs")
        seen.add(paper.paper_id)
        if paper.infrastructure_fault:
            continue
        challenger = float(paper.challenger_score or 0.0)
        incumbent = float(paper.incumbent_score or 0.0)
        if not all(math.isfinite(score) and 0.0 <= score <= 1.0 for score in (challenger, incumbent)):
            raise ValueError("paper scores must be finite values between zero and one")
        gains.append(challenger - incumbent)
    if not gains:
        raise ValueError("no eligible paper scores")
    standard_error = stdev(gains) / math.sqrt(len(gains)) if len(gains) > 1 else math.inf
    return PairedResult(len(gains), mean(gains), standard_error, tuple(gains))


def gold_veto(*, decided_claims: int, incumbent_supported: int, min_decided: int = 60) -> bool:
    """A binding Gold check vetoes when >=75% of decided claims support R."""
    if decided_claims < 0 or incumbent_supported < 0 or incumbent_supported > decided_claims:
        raise ValueError("invalid Gold vote counts")
    return decided_claims >= min_decided and 4 * incumbent_supported >= 3 * decided_claims


def crown_eligible(result: PairedResult, *, gold_refused: bool, week_one: bool = False) -> bool:
    """Statistical crown gate; other copy, team and code checks are separate."""
    if gold_refused:
        return False
    if week_one:
        return result.mean_gain > 0.0
    return result.paper_count >= 2 and result.mean_gain > 2.13 * result.standard_error
