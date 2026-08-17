from types import SimpleNamespace

from neurons.miner_selection import select_miners


def _miner(uid: int):
    return SimpleNamespace(uid=uid, hotkey=f"hotkey_{uid}", coldkey=f"coldkey_{uid}")


def test_adaptive_selection_reserves_development_and_exploration_slots() -> None:
    candidates = [_miner(uid) for uid in range(1, 21)]
    history = [
        {
            "hotkey": f"hotkey_{uid}",
            "average_score": 0.95 if uid <= 8 else 0.1,
            "report_count": 10 if uid <= 16 else 0,
            "latest_report_at": "2026-08-01T00:00:00Z" if uid <= 16 else None,
        }
        for uid in range(1, 21)
    ]

    selected = select_miners(candidates, history_rows=history, sample_size=10, seed="batch-001")

    assert len(selected) == 10
    assert len({item.hotkey for item in selected}) == 10
    assert sum(item.lane == "development" for item in selected) == 2
    assert sum(item.lane == "exploration" for item in selected) == 2
    assert {item.hotkey for item in selected if item.lane == "development"}.issubset(
        {"hotkey_17", "hotkey_18", "hotkey_19", "hotkey_20"}
    )


def test_adaptive_selection_is_reproducible_for_the_same_seed() -> None:
    candidates = [_miner(uid) for uid in range(1, 16)]
    history = [
        {"hotkey": f"hotkey_{uid}", "average_score": uid / 20, "report_count": 5}
        for uid in range(1, 16)
    ]

    first = select_miners(candidates, history_rows=history, sample_size=8, seed="stable-seed")
    second = select_miners(candidates, history_rows=history, sample_size=8, seed="stable-seed")

    assert [(item.uid, item.lane) for item in first] == [(item.uid, item.lane) for item in second]


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


def test_development_lane_counts_selection_opportunities_without_scores() -> None:
    selected = select_miners(
        [_miner(uid) for uid in range(1, 7)],
        history_rows=[
            {"hotkey": "hotkey_1", "selected_run_count": 5, "report_count": 0},
            {"hotkey": "hotkey_2", "selected_run_count": 0, "report_count": 8, "average_score": 0.9},
        ],
        sample_size=3,
        seed="opportunity-counts",
    )

    development = next(item for item in selected if item.lane == "development")
    assert development.hotkey != "hotkey_1"
