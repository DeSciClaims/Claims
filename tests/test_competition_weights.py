import pytest

from competition.weights import Payee, weekly_weight_plan


def test_weight_plan_pays_distinct_qualified_teams_in_band():
    king = Payee("king", "king-team", True, gain=0.04)
    finalists = [
        king,
        Payee("same-team", "king-team", True, gain=0.039),
        Payee("runner-a", "a", True, gain=0.03),
        Payee("runner-b", "b", True, gain=0.00),
        Payee("far", "c", True, gain=-0.02),
    ]
    plan = weekly_weight_plan(
        operator_hotkey="operator", production=king, finalists=finalists,
        uid_by_hotkey={"operator": 9, "king": 1, "same-team": 2, "runner-a": 3, "runner-b": 4, "far": 5},
        winner_gain=king.gain,
    )
    assert plan.weights_by_uid == {1: 0.07, 3: 0.02, 4: 0.01, 9: 0.90}
    assert plan.burned_fraction == 0.0


def test_unqualified_production_and_empty_runner_slots_burn():
    king = Payee("king", "king-team", False)
    plan = weekly_weight_plan(
        operator_hotkey="operator", production=king, finalists=[],
        uid_by_hotkey={"operator": 9, "king": 1},
    )
    assert plan.weights_by_uid == {0: 0.1, 9: 0.9}
    assert plan.burned_fraction == pytest.approx(0.1)


def test_no_crown_does_not_pay_far_or_copy_finalists():
    king = Payee("king", "king-team", True)
    finalists = [Payee("copy", "a", True, gain=0.02, copy=True), Payee("far", "b", True, gain=-0.06)]
    plan = weekly_weight_plan(
        operator_hotkey="operator", production=king, finalists=finalists,
        uid_by_hotkey={"operator": 9, "king": 1, "copy": 2, "far": 3},
    )
    assert plan.weights_by_uid == {0: 0.03, 1: 0.07, 9: 0.9}
    with pytest.raises(ValueError, match="reserved"):
        weekly_weight_plan(
            operator_hotkey="operator", production=king, finalists=[],
            uid_by_hotkey={"operator": 9, "king": 0},
        )


def test_reference_fallback_without_winner_burns_all_miner_shares():
    plan = weekly_weight_plan(
        operator_hotkey="operator", production=None,
        finalists=[Payee("near", "other-team", True, gain=0.02)],
        uid_by_hotkey={"operator": 9, "near": 1}, winner_gain=0.0,
    )
    assert plan.weights_by_uid == {0: 0.1, 9: 0.9}
    assert plan.burned_fraction == pytest.approx(0.1)
    assert [role for _, role, _ in plan.roles] == [
        "operator", "production_burned", "runner_up_1_burned", "runner_up_2_burned",
    ]
