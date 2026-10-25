import hashlib
import math

import pytest

from competition.policy import (
    PaperPairScore,
    commitment_hash,
    crown_eligible,
    draw_papers,
    gold_veto,
    paired_result,
)


def test_commitment_uses_exact_package_hotkey_and_salt_bytes():
    package, hotkey, salt = b"wheel bytes", bytes(range(32)), b"s" * 32
    assert commitment_hash(package, hotkey, salt) == hashlib.sha256(package + hotkey + salt).hexdigest()
    with pytest.raises(ValueError, match="salt"):
        commitment_hash(package, hotkey, b"short")


def test_draw_is_reproducible_disjoint_and_order_independent():
    papers = [f"paper-{n:04d}" for n in range(100)]
    seed = bytes.fromhex("a1" * 32)
    first = draw_papers(papers, seed=seed, prescreen_count=20, screen_count=12, final_count=50)
    second = draw_papers(list(reversed(papers)), seed=seed, prescreen_count=20, screen_count=12, final_count=50)
    assert first == second
    assert len(set(first.prescreen + first.screen + first.final)) == 82
    assert len(first.judges) == 50
    assert {judge for _, judge in first.judges} == {"J1", "J2"}
    assert all(first.judges[i][1] != first.judges[i + 1][1] for i in range(49))
    assert first != draw_papers(papers, seed=bytes.fromhex("b2" * 32), prescreen_count=20, screen_count=12, final_count=50)


def test_paired_gain_drops_infrastructure_fault_and_scores_missing_output_zero():
    result = paired_result([
        PaperPairScore("a", 0.9, 0.8),
        PaperPairScore("b", None, 0.5),
        PaperPairScore("c", 1.0, 0.0, infrastructure_fault=True),
    ])
    assert result.paper_count == 2
    assert result.gains == pytest.approx((0.1, -0.5))
    assert result.mean_gain == pytest.approx(-0.2)
    assert math.isfinite(result.standard_error)


def test_gold_and_statistical_crown_gates():
    result = paired_result([
        PaperPairScore(f"p-{n}", 0.8, 0.7)
        for n in range(500)
    ])
    assert crown_eligible(result, gold_refused=False)
    assert not crown_eligible(result, gold_refused=True)
    assert gold_veto(decided_claims=60, incumbent_supported=45)
    assert not gold_veto(decided_claims=59, incumbent_supported=59)
    assert not gold_veto(decided_claims=60, incumbent_supported=44)
    assert crown_eligible(paired_result([PaperPairScore("one", 0.8, 0.7)]), gold_refused=False, week_one=True)
