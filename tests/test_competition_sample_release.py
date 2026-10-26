import hashlib

import pytest

from competition.drand import DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, VerifiedBeacon
from competition.sample_release import PaperRecord, frozen_pool_hash, sample_receipt


def papers(count):
    return [
        PaperRecord(f"paper-{i:03d}", hashlib.sha256(f"pdf-{i}".encode()).hexdigest())
        for i in range(count)
    ]


def beacon(value: bytes = b"a" * 96) -> VerifiedBeacon:
    return VerifiedBeacon(123, hashlib.sha256(value).digest(), value.hex(),
                          DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, 1_800_000_000)


def test_post_deadline_sample_receipt_is_order_independent_and_disjoint():
    pool = papers(30)
    parameters = {
        "beacon": beacon(),
        "prescreen_count": 3, "screen_count": 5, "final_count": 8,
    }
    first = sample_receipt(papers=pool, **parameters)
    second = sample_receipt(papers=list(reversed(pool)), **parameters)
    assert first == second
    assert first["paper_pool_sha256"] == frozen_pool_hash(pool)
    assert len(first["papers"]) == 16
    assert len({row["paper_id"] for row in first["papers"]}) == 16
    assert {row["judge_id"] for row in first["papers"] if row["stage"] == "final"} == {"J1", "J2"}
    assert sample_receipt(papers=pool, **{**parameters, "beacon": beacon(b"b" * 96)}) != first


def test_week_one_single_stage_and_duplicate_pool_rejected():
    receipt = sample_receipt(
        papers=papers(10), beacon=beacon(),
        prescreen_count=0, screen_count=0, final_count=8, week_one=True,
    )
    assert {row["stage"] for row in receipt["papers"]} == {"week_one"}
    with pytest.raises(ValueError, match="unique"):
        frozen_pool_hash([papers(1)[0], papers(1)[0]])
    with pytest.raises(ValueError, match="single"):
        sample_receipt(
            papers=papers(10), beacon=beacon(),
            prescreen_count=1, screen_count=0, final_count=8, week_one=True,
        )
