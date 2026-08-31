from types import SimpleNamespace

import pytest

from neurons.miner_selection import select_miners, selection_lane_slots
from neurons.validator import ClaimsValidator, _metagraph_registration_blocks


def _miner(uid: int, *, registration_block: int = 100):
    return SimpleNamespace(
        uid=uid,
        hotkey=f"hotkey_{uid}",
        coldkey=f"coldkey_{uid}",
        block_at_registration=registration_block,
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
) -> dict:
    return {
        "uid": uid,
        "registration_block": registration_block,
        "evaluation_count": count,
        "distinct_batch_count": count if distinct_batch_count is None else distinct_batch_count,
        "recent_scores": scores or [],
        "last_selected_batch": f"batch_{uid}" if last_selected_block is not None else None,
        "last_selected_block": last_selected_block,
        "last_evaluated_block": last_evaluated_block,
    }


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


def test_immunity_deadline_overrides_normal_qualification_order() -> None:
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
        _state(1, count=2, registration_block=9_500),
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


def test_selection_is_reproducible_and_uses_uid_state_not_hotkey() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        _state(uid, count=0 if uid <= 6 else 3, scores=[uid / 20] * 3)
        for uid in range(1, 16)
    ]
    history[6]["hotkey"] = "an_unrelated_hotkey"

    first = select_miners(candidates, history_rows=history, sample_size=15, seed="stable-seed")
    second = select_miners(candidates, history_rows=history, sample_size=15, seed="stable-seed")

    assert [(item.uid, item.lane) for item in first] == [(item.uid, item.lane) for item in second]
    assert next(item for item in first if item.uid == 7).evaluation_count == 3
    assert next(item for item in first if item.uid == 7).distinct_batch_count == 3


def test_registration_block_change_discards_old_uid_state() -> None:
    selected = select_miners(
        [_miner(1, registration_block=200)],
        history_rows=[_state(1, count=20, scores=[1.0, 1.0, 1.0], registration_block=100)],
        sample_size=15,
        seed="registration-reset",
    )

    assert selected[0].status == "new"
    assert selected[0].evaluation_count == 0
    assert selected[0].recent_scores == ()


def test_lane_shortages_fill_by_oldest_evaluation() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        _state(uid, count=3, scores=[0.5] * 3, last_evaluated_block=100 + uid)
        for uid in range(1, 16)
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="fill")

    assert [item.uid for item in selected[:6]] == [1, 2, 3, 4, 5, 6]
    assert {item.lane for item in selected[:6]} == {"qualification"}


def test_positive_under_vetted_history_fills_provisional_performance_slots() -> None:
    candidates = [_miner(uid) for uid in range(1, 19)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=1, scores=[uid / 20]) for uid in range(7, 13)],
        *[_state(uid, count=0) for uid in range(13, 19)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="provisional")

    assert {item.uid for item in selected if item.lane == "performance-provisional"} == {
        7,
        8,
        9,
        10,
        11,
        12,
    }
    assert not [item for item in selected if item.lane == "performance"]
    assert all(item.status == "under-vetted" for item in selected if item.lane == "performance-provisional")


def test_zero_only_under_vetted_history_is_not_provisional_performance() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=1, scores=[0.0]) for uid in range(7, 16)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="zero-only")

    assert not [item for item in selected if item.lane == "performance-provisional"]
    assert sum(item.lane == "performance-fallback" for item in selected) == 6


def test_zero_vetted_miners_mix_provisional_performance_with_fallback() -> None:
    candidates = [_miner(uid) for uid in range(1, 19)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=1, scores=[0.5]) for uid in range(7, 11)],
        *[_state(uid, count=0) for uid in range(11, 19)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="mixed")

    assert {item.uid for item in selected if item.lane == "performance-provisional"} == {7, 8, 9, 10}
    assert [item.uid for item in selected if item.lane == "performance-fallback"] == [11, 12]


def test_provisional_history_does_not_displace_six_vetted_performers() -> None:
    candidates = [_miner(uid) for uid in range(1, 21)]
    history = [
        *[_state(uid, count=0) for uid in range(1, 7)],
        *[_state(uid, count=3, scores=[0.4, 0.5, 0.6]) for uid in range(7, 13)],
        *[_state(uid, count=1, scores=[0.9]) for uid in range(13, 21)],
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=15, seed="vetted-first")

    assert {item.uid for item in selected if item.lane == "performance"} == {7, 8, 9, 10, 11, 12}
    assert not [item for item in selected if item.lane == "performance-provisional"]


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
            {"uid": 7, "registration_block": 100},
            {"uid": 8, "registration_block": 200},
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
                {"uid": 7, "registration_block": 100, "score": 0.75},
                {"uid": 8, "registration_block": 200, "score": 0.0},
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
            "miner_selection_algorithm": "uid_v0",
            "target_miners": [{"uid": 7, "hotkey": "hotkey_7", "registration_block": 100}],
        },
        record_miner_selections=lambda **_payload: [],
    )
    validator.bt_logging = SimpleNamespace(info=lambda *_args: None)
    validator._active_miner_selection = {}

    def propose(*, selection_seed, batch_id, recent_registration_block):
        assert selection_seed == "canonical-seed"
        assert batch_id is None
        assert recent_registration_block == 900
        validator._active_miner_selection = {
            "algorithm": "uid_v0",
            "metagraph_block": 850,
            "assignments": [{"uid": 8, "hotkey": "hotkey_8", "registration_block": 100}],
        }
        return [SimpleNamespace(uid=8)]

    resolved: list[dict] = []
    validator._load_target_neurons = propose
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
