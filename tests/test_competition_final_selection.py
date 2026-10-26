import pytest

from competition.final_selection import Finalist, select_final_production
from competition.policy import PaperPairScore


def finalist(hotkey, team, gains, *, commit=10, code=True, copy=False, gold=False):
    return Finalist(
        hotkey, team, commit,
        tuple(PaperPairScore(f"paper-{index}", 0.5 + gain, 0.5)
              for index, gain in enumerate(gains)),
        code, copy, gold,
    )


def test_crown_requires_statistical_gain_and_gold_and_code_checks():
    rows = [
        finalist("good", "team-a", [0.05] * 10),
        finalist("gold", "team-b", [0.10] * 10, gold=True),
        finalist("copy", "team-c", [0.10] * 10, copy=True),
        finalist("bad-code", "team-d", [0.10] * 10, code=False),
    ]
    selection = select_final_production(rows, incumbent_hotkey="old", incumbent_team_id="old-team")
    assert selection.production_hotkey == "good"
    assert selection.decision == "new_crown"
    assert {row.hotkey: row.reason for row in selection.verdicts} == {
        "good": "cleared", "gold": "gold_refused", "copy": "copy", "bad-code": "code_failed",
    }


def test_tie_uses_se_of_difference_and_earlier_commitment():
    # The higher mean wins numerically; their paired difference is within one SE.
    earlier = finalist("earlier", "team-a", [0.04, 0.07, 0.04, 0.07], commit=5)
    later = finalist("later", "team-b", [0.07, 0.04, 0.07, 0.08], commit=6)
    selection = select_final_production([later, earlier], incumbent_hotkey="old", incumbent_team_id="old-team")
    assert selection.production_hotkey == "earlier"


def test_own_version_swaps_at_zero_but_negative_version_does_not():
    zero = finalist("new-own", "old-team", [0.0] * 5)
    swap = select_final_production([zero], incumbent_hotkey="old", incumbent_team_id="old-team")
    assert swap.production_hotkey == "new-own" and swap.decision == "incumbent_version_swap"
    negative = finalist("new-own", "old-team", [-0.01] * 5)
    keep = select_final_production([negative], incumbent_hotkey="old", incumbent_team_id="old-team")
    assert keep.production_hotkey == "old" and keep.decision == "incumbent_continues"


def test_week_one_needs_positive_gain_and_shared_grid():
    no_crown = select_final_production([finalist("a", "a-team", [0] * 5)], incumbent_hotkey=None)
    assert no_crown.production_hotkey is None
    crown = select_final_production([finalist("a", "a-team", [0.01] * 5)], incumbent_hotkey=None)
    assert crown.production_hotkey == "a"
    mismatched = finalist("b", "b-team", [0.01] * 4)
    with pytest.raises(ValueError, match="same ordered paper"):
        select_final_production([finalist("a", "a-team", [0.01] * 5), mismatched], incumbent_hotkey=None)
