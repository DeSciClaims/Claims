"""Freeze the paper pool and build a reproducible post-deadline sample receipt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .drand import VerifiedBeacon
from .policy import draw_papers


@dataclass(frozen=True)
class PaperRecord:
    paper_id: str
    source_sha256: str


def frozen_pool_hash(papers: Iterable[PaperRecord]) -> str:
    entries = sorted((paper.paper_id, paper.source_sha256) for paper in papers)
    if not entries or len({paper_id for paper_id, _ in entries}) != len(entries):
        raise ValueError("paper pool must be nonempty with unique IDs")
    if any(not paper_id or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
           for paper_id, digest in entries):
        raise ValueError("paper pool has an invalid ID or SHA-256 digest")
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def sample_receipt(
    *, papers: list[PaperRecord], beacon: VerifiedBeacon,
    prescreen_count: int, screen_count: int, final_count: int,
    week_one: bool = False,
) -> dict[str, Any]:
    """Use only a verified drand seed obtained after the deadline reveal block."""
    if beacon.round < 1 or len(beacon.seed) != 32:
        raise ValueError("verified drand round and 32-byte seed are required")
    if hashlib.sha256(bytes.fromhex(beacon.signature_hex)).digest() != beacon.seed:
        raise ValueError("drand seed differs from verified signature")
    paper_map = {paper.paper_id: paper for paper in papers}
    pool_sha256 = frozen_pool_hash(papers)
    if week_one and (prescreen_count or screen_count):
        raise ValueError("week one is a single full-pipeline stage")
    draw = draw_papers(
        list(paper_map), seed=beacon.seed,
        prescreen_count=prescreen_count,
        screen_count=screen_count,
        final_count=final_count,
    )
    rows: list[dict[str, Any]] = []
    for position, paper_id in enumerate(draw.prescreen):
        rows.append({
            "stage": "prescreen", "paper_id": paper_id, "position": position,
            "judge_id": "routing", "source_sha256": paper_map[paper_id].source_sha256,
        })
    screen_first = hashlib.sha256(b"claims-weekly-screen-judge-v1\0" + beacon.seed).digest()[0] & 1
    for position, paper_id in enumerate(draw.screen):
        rows.append({
            "stage": "screen", "paper_id": paper_id, "position": position,
            "judge_id": ("J1", "J2")[(screen_first + position) % 2],
            "source_sha256": paper_map[paper_id].source_sha256,
        })
    for position, (paper_id, judge_id) in enumerate(draw.judges):
        rows.append({
            "stage": "week_one" if week_one else "final",
            "paper_id": paper_id, "position": position,
            "judge_id": judge_id, "source_sha256": paper_map[paper_id].source_sha256,
        })
    return {
        "drand_round": beacon.round,
        "drand_seed": beacon.seed.hex(),
        "drand_chain_hash": beacon.chain_hash,
        "drand_public_key": beacon.public_key,
        "drand_signature": beacon.signature_hex,
        "drand_scheduled_at": beacon.scheduled_at,
        "paper_pool_sha256": pool_sha256,
        "papers": rows,
    }
