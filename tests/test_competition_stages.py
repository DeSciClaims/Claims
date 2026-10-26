import pytest

from competition.stages import StageCandidate, advance, stage_plan


def test_stage_plan_matches_field_size_and_week_one():
    assert stage_plan(challenger_count=0) == ()
    assert stage_plan(challenger_count=2) == ("final",)
    assert stage_plan(challenger_count=12) == ("screen", "final")
    assert stage_plan(challenger_count=13) == ("prescreen", "screen", "final")
    assert stage_plan(challenger_count=13, week_one=True) == ("week_one",)


def test_prescreen_and_screen_team_caps_and_stable_ranking():
    entries = [StageCandidate(f"h{index:02d}", "a" if index < 5 else f"team-{index}", 1 - index / 100, index) for index in range(20)]
    pre = advance(entries, stage="prescreen")
    assert len(pre) == 12
    assert [candidate.hotkey for candidate in pre if candidate.team_id == "a"] == ["h00", "h01"]
    assert pre == advance(list(reversed(entries)), stage="prescreen")
    final = advance(entries, stage="screen")
    assert [candidate.hotkey for candidate in final] == ["h00", "h05", "h06"]


def test_stage_ties_use_earlier_commit_then_hotkey():
    entries = [
        StageCandidate("z", "z", 0.1, 9),
        StageCandidate("b", "b", 0.1, 8),
        StageCandidate("a", "a", 0.1, 8),
        StageCandidate("x", "x", 0.2, 10),
    ]
    assert [item.hotkey for item in advance(entries, stage="screen")] == ["x", "a", "b"]
    with pytest.raises(ValueError, match="distinct"):
        advance(entries + [entries[0]], stage="screen")
