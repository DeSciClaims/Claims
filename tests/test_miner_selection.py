from types import SimpleNamespace

import pytest

from neurons.miner_selection import select_miners, selection_lane_slots
from neurons.validator import ClaimsValidator, _metagraph_registration_blocks


def _miner(
    uid: int,
    *,
    registration_block: int = 100,
    hotkey: str | None = None,
    coldkey: str | None = None,
    axon_ip: str | None = None,
):
    return SimpleNamespace(
        uid=uid,
        hotkey=hotkey or f"hotkey_{uid}",
        coldkey=coldkey or f"coldkey_{uid}",
        block_at_registration=registration_block,
        axon_info=SimpleNamespace(ip=axon_ip or f"198.{uid}.0.1"),
    )


def _state(
    uid: int,
    *,
    count: int,
    scores: list[float] | None = None,
    registration_block: int = 100,
    last_selected_block: int | None = None,
    last_evaluated_block: int | None = None,
    distinct_batch_count: int | None = None,
    hotkey: str | None = None,
    coldkey_evaluation_count: int = 0,
    coldkey_qualification_count: int = 0,
) -> dict:
    return {
        "uid": uid,
        "hotkey": hotkey or f"hotkey_{uid}",
        "registration_block": registration_block,
        "evaluation_count": count,
        "coldkey_evaluation_count": coldkey_evaluation_count,
        "coldkey_qualification_count": coldkey_qualification_count,
        "distinct_batch_count": count if distinct_batch_count is None else distinct_batch_count,
        "recent_scores": scores or [],
        "last_selected_batch": f"batch_{uid}" if last_selected_block is not None else None,
        "last_selected_block": last_selected_block,
        "last_evaluated_block": last_evaluated_block,
    }


def test_bucket_policy_selects_fifo_newcomers_and_eight_established_miners() -> None:
    candidates = [_miner(uid) for uid in range(1, 22)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 6)],
        *[
            _state(
                uid,
                count=2,
                scores=[uid / 30, uid / 25],
                last_evaluated_block=uid * 10,
            )
            for uid in range(6, 21)
        ],
        _state(21, count=0),
    ]
    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="bucket-7",
        mode="bucket",
        current_block=2_000,
    )

    assert len(selected) == 13
    assert [item.uid for item in selected if item.lane == "qualification"] == [1, 2, 3, 4, 5]
    performance_uids = [item.uid for item in selected if item.lane == "performance"]
    repeated = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="bucket-7",
        mode="bucket",
        current_block=2_000,
    )
    assert len(performance_uids) == 4
    assert performance_uids == [item.uid for item in repeated if item.lane == "performance"]
    assert set(performance_uids) != {17, 18, 19, 20}
    assert sum(item.lane == "rotation" for item in selected) == 4
    assert 21 not in {item.uid for item in selected}


def test_bucket_policy_rejects_newcomer_when_its_coldkey_has_evaluation_history() -> None:
    candidates = [
        _miner(1, coldkey="shared_coldkey"),
        _miner(2, coldkey="shared_coldkey"),
        *[_miner(uid) for uid in range(3, 14)],
    ]
    history = [
        _state(1, count=0, coldkey_evaluation_count=1),
        _state(2, count=1, scores=[0.8], last_evaluated_block=100),
        *[
            _state(uid, count=1, scores=[0.5], last_evaluated_block=100 + uid)
            for uid in range(3, 14)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="evaluated-coldkey",
        mode="bucket",
        current_block=2_000,
    )

    assert 1 not in {item.uid for item in selected if item.lane == "qualification"}
    assert sum(item.coldkey == "shared_coldkey" for item in selected) <= 1


def test_bucket_policy_does_not_repeat_a_claimed_newcomer_opportunity() -> None:
    candidates = [_miner(uid) for uid in range(1, 12)]
    history = [
        _state(1, count=0, coldkey_qualification_count=1),
        *[_state(uid, count=0) for uid in range(2, 7)],
        *[
            _state(uid, count=1, scores=[0.5], last_evaluated_block=100 + uid)
            for uid in range(7, 12)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="claimed-newcomer",
        mode="bucket",
        current_block=2_000,
    )

    assert 1 not in {item.uid for item in selected if item.lane == "qualification"}
    assert [item.uid for item in selected if item.lane == "qualification"] == [2, 3, 4, 5, 6]


def test_bucket_policy_uses_earliest_hotkey_for_each_unevaluated_coldkey() -> None:
    candidates = [
        _miner(1, registration_block=200, coldkey="shared_coldkey"),
        _miner(2, registration_block=100, coldkey="shared_coldkey"),
        *[_miner(uid, registration_block=100 + uid) for uid in range(3, 16)],
    ]
    history = [
        _state(uid, count=0 if uid <= 7 else 1, scores=[] if uid <= 7 else [0.5])
        for uid in range(1, 16)
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="coldkey-representative",
        mode="bucket",
        current_block=2_000,
    )

    newcomers = [item.uid for item in selected if item.lane == "qualification"]
    assert 2 in newcomers
    assert 1 not in newcomers
    assert len(newcomers) == 5


def test_uid_v0_selection_uses_exact_six_six_three_lanes() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        *[_state(uid, count=(uid - 1) // 2, last_selected_block=uid) for uid in range(1, 7)],
        *[
            _state(uid, count=3, scores=[uid / 20, uid / 20, uid / 20], last_evaluated_block=uid * 10)
            for uid in range(7, 16)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="batch-001",
        current_block=1_000,
        immunity_period_blocks=21_600,
    )

    assert len(selected) == 15
    assert len({item.uid for item in selected}) == 15
    assert [item.uid for item in selected if item.lane == "qualification"] == [1, 2, 3, 4, 5, 6]
    assert sum(item.lane == "performance" for item in selected) == 6
    assert sum(item.lane == "rotation" for item in selected) == 3
    rotation = [item for item in selected if item.lane == "rotation"]
    assert rotation == sorted(rotation, key=lambda item: (item.last_evaluated_block, item.uid))


def test_positive_history_immunity_deadline_overrides_normal_qualification_order() -> None:
    candidates = [
        _miner(1, registration_block=9_500),
        _miner(2, registration_block=20_000),
        _miner(3, registration_block=20_000),
        _miner(4, registration_block=20_000),
        _miner(5, registration_block=20_000),
        _miner(6, registration_block=20_000),
        _miner(7, registration_block=20_000),
        *[_miner(uid) for uid in range(8, 19)],
    ]
    history = [
        _state(1, count=2, scores=[0.5, 0.5], registration_block=9_500),
        *[_state(uid, count=0, registration_block=20_000) for uid in range(2, 8)],
        *[_state(uid, count=3, scores=[0.5, 0.5, 0.5]) for uid in range(8, 19)],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="immunity",
        current_block=24_000,
        immunity_period_blocks=21_600,
        immunity_priority_blocks=7_200,
    )

    qualification = [item.uid for item in selected if item.lane == "qualification"]
    assert qualification[0] == 1
    assert len(qualification) == 6


def test_all_zero_history_ranks_behind_never_evaluated_despite_immunity_deadline() -> None:
    candidates = [
        _miner(1, registration_block=9_500),
        *[_miner(uid, registration_block=20_000) for uid in range(2, 8)],
        *[_miner(uid) for uid in range(8, 19)],
    ]
    history = [
        _state(1, count=2, scores=[0.0, 0.0], registration_block=9_500),
        *[_state(uid, count=0, registration_block=20_000) for uid in range(2, 8)],
        *[_state(uid, count=3, scores=[0.5, 0.5, 0.5]) for uid in range(8, 19)],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="zero-immunity",
        current_block=24_000,
        immunity_period_blocks=21_600,
        immunity_priority_blocks=7_200,
    )

    qualification = [item.uid for item in selected if item.lane == "qualification"]
    assert qualification == [2, 3, 4, 5, 6, 7]
    assert 1 not in {item.uid for item in selected if item.lane == "performance"}


def test_latest_zero_score_enforces_cooldown_across_all_lanes() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        _state(1, count=2, scores=[0.8, 0.0], last_evaluated_block=900),
        *[
            _state(uid, count=3, scores=[0.5, 0.5, 0.5], last_evaluated_block=800 + uid)
            for uid in range(2, 16)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="zero-cooldown",
        current_block=1_000,
        zero_score_cooldown_blocks=200,
    )

    assert len(selected) == 14
    assert 1 not in {item.uid for item in selected}


def test_latest_zero_score_can_return_after_cooldown_but_not_through_performance() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        _state(1, count=2, scores=[0.8, 0.0], last_evaluated_block=900),
        *[
            _state(uid, count=3, scores=[0.5, 0.5, 0.5], last_evaluated_block=800 + uid)
            for uid in range(2, 16)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="zero-cooldown-expired",
        current_block=1_100,
        zero_score_cooldown_blocks=200,
    )

    recovered = next(item for item in selected if item.uid == 1)
    assert recovered.lane != "performance"


def test_latest_positive_score_restores_performance_eligibility() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=2, scores=[0.0, 0.5], last_evaluated_block=900) for uid in range(7, 13)],
        *[_state(uid, count=0) for uid in range(13, 16)],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="positive-recovery",
        current_block=1_000,
        zero_score_cooldown_blocks=7_200,
    )

    assert {item.uid for item in selected if item.lane == "performance"} == set(range(7, 13))


def test_selection_is_reproducible_and_uses_hotkey_state() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        _state(uid, count=0 if uid <= 6 else 3, scores=[uid / 20] * 3)
        for uid in range(1, 16)
    ]
    first = select_miners(candidates, history_rows=history, sample_size=15, seed="stable-seed")
    second = select_miners(candidates, history_rows=history, sample_size=15, seed="stable-seed")

    assert [(item.uid, item.lane) for item in first] == [(item.uid, item.lane) for item in second]
    assert next(item for item in first if item.uid == 7).evaluation_count == 3
    assert next(item for item in first if item.uid == 7).distinct_batch_count == 3


def test_registration_block_change_preserves_same_hotkey_state() -> None:
    selected = select_miners(
        [_miner(1, registration_block=200)],
        history_rows=[_state(1, count=20, scores=[1.0, 1.0, 1.0], registration_block=100)],
        sample_size=15,
        seed="hotkey-history",
    )

    assert selected[0].status == "vetted"
    assert selected[0].evaluation_count == 20
    assert selected[0].recent_scores == (1.0, 1.0, 1.0)


def test_new_hotkey_on_reused_uid_does_not_inherit_history() -> None:
    selected = select_miners(
        [_miner(1, registration_block=200, hotkey="replacement_hotkey")],
        history_rows=[_state(1, count=20, scores=[1.0, 1.0, 1.0], registration_block=100)],
        sample_size=15,
        seed="hotkey-replacement",
    )

    assert selected[0].status == "new"
    assert selected[0].evaluation_count == 0
    assert selected[0].recent_scores == ()


def test_adaptive_draw_caps_each_coldkey_and_axon_ip_to_one_seat() -> None:
    candidates = [
        _miner(1, coldkey="shared_coldkey", axon_ip="198.51.100.1"),
        _miner(2, coldkey="shared_coldkey", axon_ip="198.51.100.2"),
        _miner(3, coldkey="coldkey_3", axon_ip="198.51.100.1"),
        *[
            _miner(uid, coldkey=f"coldkey_{uid}", axon_ip=f"198.51.100.{uid}")
            for uid in range(4, 19)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=15,
        seed="diversity-cap",
        ipv4_proximity_addresses=0,
    )

    assert len(selected) == 15
    assert len({item.coldkey for item in selected}) == 15
    assert len({item.axon_ip for item in selected}) == 15
    assert len({item.uid for item in selected}.intersection({1, 2})) == 1
    assert len({item.uid for item in selected}.intersection({1, 3})) == 1


def test_adaptive_draw_returns_fewer_miners_when_diversity_cap_exhausts_pool() -> None:
    candidates = [
        _miner(uid, coldkey=f"coldkey_{uid % 2}", axon_ip=f"198.51.100.{uid % 2}")
        for uid in range(1, 11)
    ]

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=10,
        seed="limited-diversity",
        ipv4_proximity_addresses=0,
    )

    assert len(selected) == 2
    assert len({item.coldkey for item in selected}) == 2
    assert len({item.axon_ip for item in selected}) == 2


def test_performance_draw_enforces_coldkey_and_axon_ip_caps() -> None:
    candidates = [
        _miner(1, coldkey="shared_coldkey", axon_ip="198.51.100.1"),
        _miner(2, coldkey="shared_coldkey", axon_ip="198.51.100.2"),
        _miner(3, coldkey="coldkey_3", axon_ip="198.51.100.1"),
        *[
            _miner(uid, coldkey=f"coldkey_{uid}", axon_ip=f"198.51.100.{uid}")
            for uid in range(4, 19)
        ],
    ]
    history = [
        _state(uid, count=3, scores=[0.9, 0.8, 0.7])
        for uid in range(1, 19)
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="performance-diversity-cap",
        ipv4_proximity_addresses=0,
    )

    assert len(selected) == 15
    assert len({item.coldkey for item in selected}) == 15
    assert len({item.axon_ip for item in selected}) == 15


def test_adaptive_draw_rejects_nearby_ipv4_axons_across_adjacent_ranges() -> None:
    candidates = [
        _miner(1, axon_ip="95.133.252.10"),
        _miner(2, axon_ip="95.133.253.20"),
        _miner(3, axon_ip="95.133.254.30"),
        _miner(4, axon_ip="95.140.0.1"),
    ]
    diagnostics: list[dict] = []

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=4,
        seed="adjacent-ip-ranges",
        ipv4_proximity_addresses=1_024,
        selection_diagnostics=diagnostics,
    )

    assert [item.uid for item in selected] == [1, 4]
    proximity_exclusions = {
        item["uid"]: item
        for item in diagnostics
        if item["reason"] == "axon_ipv4_proximity_conflict"
    }
    assert set(proximity_exclusions) == {2, 3}
    assert all(item["conflicting_uid"] == 1 for item in proximity_exclusions.values())
    assert proximity_exclusions[2]["coldkey"] == "coldkey_2"
    assert proximity_exclusions[2]["conflicting_hotkey"] == "hotkey_1"
    assert proximity_exclusions[2]["conflicting_coldkey"] == "coldkey_1"


def test_zero_ipv4_proximity_uses_exact_ip_cap_only() -> None:
    candidates = [
        _miner(1, axon_ip="95.133.252.10"),
        _miner(2, axon_ip="95.133.253.20"),
        _miner(3, axon_ip="95.133.254.30"),
    ]

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=3,
        seed="exact-ip-only",
        ipv4_proximity_addresses=0,
    )

    assert [item.uid for item in selected] == [1, 2, 3]


def test_adaptive_draw_caps_ipv6_prefix_and_normalizes_addresses() -> None:
    candidates = [
        _miner(1, axon_ip="2001:0db8:0001:0002::1"),
        _miner(2, axon_ip="2001:db8:1:2::2"),
        _miner(3, axon_ip="2001:db8:1:3::1"),
    ]
    diagnostics: list[dict] = []

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=3,
        seed="ipv6-prefix",
        ipv6_prefix_bits=64,
        selection_diagnostics=diagnostics,
    )

    assert [(item.uid, item.axon_ip) for item in selected] == [
        (1, "2001:db8:1:2::1"),
        (3, "2001:db8:1:3::1"),
    ]
    exclusion = next(item for item in diagnostics if item["uid"] == 2)
    assert exclusion["reason"] == "axon_ipv6_prefix_conflict"
    assert exclusion["coldkey"] == "coldkey_2"
    assert exclusion["conflicting_coldkey"] == "coldkey_1"
    assert exclusion["network"] == "2001:db8:1:2::/64"


def test_lane_shortages_fill_by_oldest_evaluation() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        _state(uid, count=3, scores=[0.5] * 3, last_evaluated_block=100 + uid)
        for uid in range(1, 16)
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="fill")

    assert [item.uid for item in selected[:6]] == [1, 2, 3, 4, 5, 6]
    assert {item.lane for item in selected[:6]} == {"qualification"}


def test_one_evaluation_history_enters_performance_lane() -> None:
    candidates = [_miner(uid) for uid in range(1, 19)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=1, scores=[uid / 20]) for uid in range(7, 13)],
        *[_state(uid, count=0) for uid in range(13, 19)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="one-eval-performance")

    assert {item.uid for item in selected if item.lane == "performance"} == {
        7,
        8,
        9,
        10,
        11,
        12,
    }
    assert all(item.status == "under-vetted" for item in selected if item.lane == "performance")


def test_zero_score_evaluation_is_excluded_from_performance() -> None:
    candidates = [_miner(uid) for uid in range(1, 22)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 13)],
        *[_state(uid, count=1, scores=[0.0]) for uid in range(13, 22)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="zero-only")

    assert [item.uid for item in selected if item.lane == "qualification"] == [1, 2, 3, 4, 5, 6]
    assert [item for item in selected if item.lane == "performance"] == []
    assert [item.uid for item in selected if item.lane == "performance-fallback"] == [7, 8, 9, 10, 11, 12]


def test_positive_score_miners_are_preferred_over_zero_score_miners_for_performance() -> None:
    candidates = [_miner(uid) for uid in range(1, 19)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=1, scores=[0.0]) for uid in range(7, 13)],
        *[_state(uid, count=1, scores=[0.5]) for uid in range(13, 19)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="positive-only")

    assert {item.uid for item in selected if item.lane == "performance"} == set(range(13, 19))


def test_performance_shortage_still_fills_by_oldest_evaluation() -> None:
    candidates = [_miner(uid) for uid in range(1, 19)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=1, scores=[0.5]) for uid in range(7, 11)],
        *[_state(uid, count=0) for uid in range(11, 19)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="mixed")

    assert {item.uid for item in selected if item.lane == "performance"} == {7, 8, 9, 10}
    assert [item.uid for item in selected if item.lane == "performance-fallback"] == [11, 12]


def test_one_and_three_evaluation_miners_share_performance_pool() -> None:
    candidates = [_miner(uid) for uid in range(1, 21)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=3, scores=[0.4, 0.5, 0.6]) for uid in range(7, 13)],
        *[_state(uid, count=1, scores=[0.9]) for uid in range(13, 21)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="vetted-first")

    performance = [item for item in selected if item.lane == "performance"]
    assert len(performance) == 6
    assert {item.uid for item in performance}.issubset(set(range(7, 21)))
    assert any(item.status == "under-vetted" for item in performance)
    assert any(item.status == "vetted" for item in performance)


def test_cold_start_fallback_prefers_post_update_registrations_then_smallest_uid() -> None:
    candidates = [
        *[_miner(uid, registration_block=100) for uid in range(1, 7)],
        *[_miner(uid, registration_block=1_000 + uid) for uid in range(7, 21)],
    ]

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=15,
        seed="cold-start",
        recent_registration_block=1_000,
    )

    assert [item.uid for item in selected if item.lane == "qualification"] == [1, 2, 3, 4, 5, 6]
    assert [item.uid for item in selected if item.lane == "performance-fallback"] == [7, 8, 9, 10, 11, 12]
    assert [item.uid for item in selected if item.lane == "rotation"] == [13, 14, 15]


def test_cold_start_fallback_uses_pre_update_uids_only_after_recent_cohort() -> None:
    candidates = [
        *[_miner(uid, registration_block=100) for uid in range(1, 15)],
        _miner(15, registration_block=1_015),
        _miner(16, registration_block=1_016),
    ]

    selected = select_miners(
        candidates,
        history_rows=[],
        sample_size=15,
        seed="cold-start",
        recent_registration_block=1_000,
    )

    performance_fallback = [item.uid for item in selected if item.lane == "performance-fallback"]
    assert performance_fallback[:2] == [15, 16]
    assert performance_fallback[2:] == [7, 8, 9, 10]


def test_fallback_keeps_oldest_evaluation_ahead_of_recent_registration() -> None:
    candidates = [
        _miner(1, registration_block=100),
        *[_miner(uid, registration_block=1_000 + uid) for uid in range(2, 16)],
    ]
    history = [
        _state(1, count=3, scores=[0.5] * 3, registration_block=100),
        *[
            _state(
                uid,
                count=3,
                scores=[0.5] * 3,
                registration_block=1_000 + uid,
                last_evaluated_block=100 + uid,
            )
            for uid in range(2, 16)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=15,
        seed="oldest-first",
        recent_registration_block=1_000,
    )

    assert selected[0].lane == "qualification"
    assert selected[0].uid == 1


def test_all_mode_selects_every_candidate() -> None:
    selected = select_miners(
        [_miner(1), _miner(2)],
        history_rows=[],
        sample_size=1,
        seed="unused",
        mode="all",
    )

    assert [item.uid for item in selected] == [1, 2]
    assert {item.lane for item in selected} == {"all"}


@pytest.mark.parametrize(
    ("sample_size", "expected_slots"),
    [
        (10, (4, 4, 2)),
        (15, (6, 6, 3)),
        (20, (8, 8, 4)),
        (7, (3, 3, 1)),
    ],
)
def test_adaptive_lane_slots_scale_with_sample_size(
    sample_size: int,
    expected_slots: tuple[int, int, int],
) -> None:
    assert selection_lane_slots(sample_size) == expected_slots


@pytest.mark.parametrize("sample_size", [10, 20])
def test_adaptive_mode_accepts_configured_sample_size(sample_size: int) -> None:
    qualification_slots, performance_slots, rotation_slots = selection_lane_slots(sample_size)
    candidates = [_miner(uid) for uid in range(1, sample_size + 1)]
    history = [
        *[_state(uid, count=0) for uid in range(1, qualification_slots + 1)],
        *[
            _state(uid, count=3, scores=[0.5, 0.5, 0.5], last_evaluated_block=uid)
            for uid in range(qualification_slots + 1, sample_size + 1)
        ],
    ]

    selected = select_miners(
        candidates,
        history_rows=history,
        sample_size=sample_size,
        seed=f"sample-{sample_size}",
    )

    assert len(selected) == sample_size
    assert sum(item.lane == "qualification" for item in selected) == qualification_slots
    assert sum(item.lane == "performance" for item in selected) == performance_slots
    assert sum(item.lane == "rotation" for item in selected) == rotation_slots


def test_metagraph_registration_blocks_are_mapped_by_uid() -> None:
    metagraph = SimpleNamespace(block_at_registration=[10, 20, 30])

    assert _metagraph_registration_blocks(metagraph, [_miner(2), _miner(0)]) == {2: 30, 0: 10}


@pytest.mark.parametrize("mode", ["adaptive", "override", "all"])
def test_completed_scores_record_every_selected_uid_including_zero(mode: str) -> None:
    calls: list[dict] = []
    backend = SimpleNamespace(
        record_miner_selection_evaluations=lambda **payload: calls.append(payload)
        or {"recorded": 2, "duplicate": 0, "stale": 0}
    )
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.backend_client = backend
    validator.config = SimpleNamespace(netuid=530)
    validator.bt_logging = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    validator._latest_chain_block = lambda: 50_000
    validator._active_miner_selection = {
        "mode": mode,
        "assignments": [
            {"uid": 7, "hotkey": "hotkey_7", "coldkey": "coldkey_7", "registration_block": 100},
            {"uid": 8, "hotkey": "hotkey_8", "coldkey": "coldkey_8", "registration_block": 200},
        ],
    }

    validator._record_miner_selection_evaluations(
        task=SimpleNamespace(batch_id="batch_1", task_id="task_1"),
        scores={7: 0.75, 8: 0.0},
    )

    assert calls == [
        {
            "netuid": 530,
            "batch_id": "batch_1",
            "evaluated_block": 50_000,
            "evaluations": [
                {
                    "uid": 7,
                    "hotkey": "hotkey_7",
                    "coldkey": "coldkey_7",
                    "registration_block": 100,
                    "score": 0.75,
                },
                {
                    "uid": 8,
                    "hotkey": "hotkey_8",
                    "coldkey": "coldkey_8",
                    "registration_block": 200,
                    "score": 0.0,
                },
            ],
        }
    ]


def test_eligible_miner_requires_serving_axon_but_not_absence_of_validator_permit() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator_hotkey"))

    serving = SimpleNamespace(
        is_null=False,
        hotkey="miner_hotkey",
        validator_permit=False,
        axon_info=SimpleNamespace(ip="203.0.113.10", port=8091, is_serving=True),
    )
    validator_uid = SimpleNamespace(**{**serving.__dict__, "validator_permit": True})
    validator_self = SimpleNamespace(**{**serving.__dict__, "hotkey": "validator_hotkey"})
    missing_ip = SimpleNamespace(
        **{**serving.__dict__, "axon_info": SimpleNamespace(ip="0.0.0.0", port=8091, is_serving=False)}
    )

    assert validator._is_eligible_miner(serving) is True
    assert validator._is_eligible_miner(validator_uid) is True
    assert validator._is_eligible_miner(validator_self) is False
    assert validator._is_eligible_miner(missing_ip) is False


def test_task_selection_claims_one_canonical_miner_assignment() -> None:
    calls: list[dict] = []
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        netuid=530,
        claims_target_uids=[],
    )
    validator.backend_client = SimpleNamespace(
        claim_batch_miner_selection=lambda **payload: calls.append(payload)
        or {
            "created": False,
            "metagraph_block": 900,
            "miner_selection_algorithm": "bucket_fifo_v1",
            "target_miners": [{"uid": 7, "hotkey": "hotkey_7", "registration_block": 100}],
        },
        record_miner_selections=lambda **_payload: [],
    )
    validator.bt_logging = SimpleNamespace(info=lambda *_args: None)
    validator._active_miner_selection = {}

    def propose(*, selection_seed, batch_id, recent_registration_block):
        assert selection_seed == "canonical-seed"
        assert batch_id == "batch_1"
        assert recent_registration_block == 900
        validator._active_miner_selection = {
            "algorithm": "bucket_fifo_v1",
            "metagraph_block": 850,
            "assignments": [{"uid": 8, "hotkey": "hotkey_8", "registration_block": 100}],
        }
        return [SimpleNamespace(uid=8)]

    resolved: list[dict] = []
    validator._load_target_neurons = propose
    validator._miner_registration_price_tao = lambda: 1.25
    validator._miner_reward_snapshot = lambda: {
        "observed_block": 850,
        "tempo_blocks": 360,
        "alpha_out_emission_per_block": 1.0,
        "owner_cut_fraction": 0.18,
        "tao_reserve": 6_500.0,
        "alpha_reserve": 1_000_000.0,
    }
    validator._resolve_canonical_target_neurons = lambda assignments, **_kwargs: resolved.extend(assignments) or [
        SimpleNamespace(uid=7)
    ]
    task = SimpleNamespace(
        batch_id="batch_1",
        task_id="task_1",
        assignment_key="assignment_1",
        selection_seed="canonical-seed",
        miner_selection_recent_registration_block=900,
        target_miners=(),
        metagraph_block=None,
        miner_selection_algorithm="",
    )

    selected = validator._load_task_target_neurons(task, fallback_seed="run-specific-seed")

    assert [item.uid for item in selected] == [7]
    assert resolved[0]["uid"] == 7
    assert calls[0]["batch_id"] == "batch_1"
    assert calls[0]["target_miners"][0]["uid"] == 8
    assert calls[0]["registration_price_tao"] == 1.25
    assert calls[0]["miner_reward_snapshot"]["observed_block"] == 850


def test_miner_reward_snapshot_uses_live_emission_owner_cut_and_pool_reserves() -> None:
    from neurons.validator import _miner_reward_snapshot_from_chain

    class Balance:
        def __init__(self, value: float) -> None:
            self.tao = value

    snapshot = _miner_reward_snapshot_from_chain(
        SimpleNamespace(
            block=8_979_594,
            tempo=360,
            alpha_out_emission=Balance(1.0),
            tao_in=Balance(6_500.0),
            alpha_in=Balance(1_000_000.0),
        ),
        SimpleNamespace(value=11_796),
    )

    assert snapshot == {
        "observed_block": 8_979_594,
        "tempo_blocks": 360,
        "alpha_out_emission_per_block": 1.0,
        "owner_cut_fraction": 11_796 / 65_535,
        "tao_reserve": 6_500.0,
        "alpha_reserve": 1_000_000.0,
    }


def test_miner_registration_price_uses_subnet_recycle_balance() -> None:
    calls: list[int] = []
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(netuid=111, claims_bucket_registration_price_tao=0.0)
    validator.subtensor = SimpleNamespace(
        recycle=lambda *, netuid: calls.append(netuid) or SimpleNamespace(tao=0.881344005)
    )
    validator.bt_logging = SimpleNamespace(warning=lambda *_args: None)

    assert validator._miner_registration_price_tao() == pytest.approx(0.881344005)
    assert calls == [111]


def test_miner_registration_price_uses_configured_fallback_on_chain_error() -> None:
    warnings: list[str] = []
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(netuid=111, claims_bucket_registration_price_tao=0.75)

    def fail_recycle(*, netuid: int):
        raise RuntimeError(f"burn unavailable for {netuid}")

    validator.subtensor = SimpleNamespace(recycle=fail_recycle)
    validator.bt_logging = SimpleNamespace(warning=warnings.append)

    assert validator._miner_registration_price_tao() == pytest.approx(0.75)
    assert "subnet Burn value" in warnings[0]


def test_canonical_assignment_preserves_unavailable_uid_for_zero_scoring() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator_hotkey"))
    validator.metagraph = SimpleNamespace(neurons=[], hotkeys=[], block=1_000, block_at_registration=[])
    validator._sync_metagraph = lambda: None
    validator._load_neurons_by_uid = lambda: []
    warnings: list[str] = []
    validator.bt_logging = SimpleNamespace(
        warning=warnings.append,
        info=lambda *_args: None,
    )

    selected = validator._resolve_canonical_target_neurons(
        [{"uid": 7, "hotkey": "hotkey_7", "registration_block": 100}],
        selection_seed="seed",
        selected_block=1_000,
        algorithm="uid_v0",
    )

    assert [item.uid for item in selected] == [7]
    assert validator._active_unavailable_target_uids == {7: "absent from the metagraph"}
    assert selected[0].axon_info.is_serving is False
    assert "preserving assignment with zero score" in warnings[0]
