"""A replay derives its own weight plan from explicit chain/qualification inputs."""

import pytest

from competition.final_selection import FinalSelection, FinalistVerdict
from competition.replay_weights import replay_weight_plan


def _selection(hotkey="miner-1", decision="new_crown"):
    return FinalSelection(
        hotkey, decision, 0.2 if hotkey == "miner-1" else 0.0,
        (FinalistVerdict("miner-1", "team-1", 0.2, 0.01, 500,
                         True, "cleared"),),
    )


def test_replay_builds_weekly_vector_from_local_winner():
    plan = replay_weight_plan(
        _selection(), operator_hotkey="operator",
        incumbent_hotkey=None, incumbent_team_id=None,
        qualified_by_hotkey={"miner-1": True},
        uid_by_hotkey={"operator": 1, "miner-1": 2},
    )
    assert plan.weights_by_uid == {0: 0.03, 1: 0.9, 2: 0.07}
    assert plan.burned_fraction == 0.03


def test_replay_burns_unqualified_incumbent_share():
    selection = FinalSelection(
        "incumbent", "incumbent_continues", 0.0,
        (FinalistVerdict("miner-1", "team-1", -0.1, 0.01, 500,
                         False, "below_crown_bar"),),
    )
    plan = replay_weight_plan(
        selection, operator_hotkey="operator",
        incumbent_hotkey="incumbent", incumbent_team_id="team-incumbent",
        qualified_by_hotkey={"miner-1": True, "incumbent": False},
        uid_by_hotkey={"operator": 1, "incumbent": 2, "miner-1": 3},
    )
    assert plan.weights_by_uid == {0: 0.1, 1: 0.9}


def test_replay_rejects_missing_qualification_or_unearned_payee():
    with pytest.raises(ValueError, match="qualification is missing"):
        replay_weight_plan(
            _selection(), operator_hotkey="operator",
            incumbent_hotkey=None, incumbent_team_id=None,
            qualified_by_hotkey={}, uid_by_hotkey={"operator": 1},
        )
    with pytest.raises(ValueError, match="not a frozen finalist or incumbent"):
        replay_weight_plan(
            _selection("other"), operator_hotkey="operator",
            incumbent_hotkey=None, incumbent_team_id=None,
            qualified_by_hotkey={"miner-1": True},
            uid_by_hotkey={"operator": 1, "miner-1": 2},
        )
    incumbent = FinalSelection(
        "incumbent", "incumbent_continues", 0.0,
        (FinalistVerdict("miner-1", "team-1", -0.1, 0.01, 500,
                         False, "below_crown_bar"),),
    )
    with pytest.raises(ValueError, match="incumbent qualification is missing"):
        replay_weight_plan(
            incumbent, operator_hotkey="operator",
            incumbent_hotkey="incumbent", incumbent_team_id="team-incumbent",
            qualified_by_hotkey={"miner-1": True},
            uid_by_hotkey={"operator": 1, "incumbent": 2, "miner-1": 3},
        )
