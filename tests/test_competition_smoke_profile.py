"""The small rehearsal stays bound to testnet and the frozen public rules."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from competition.drand import DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, VerifiedBeacon
from competition.round_controller import reveal_selected_sample
from competition.sample_release import PaperRecord
from competition.smoke_profile import smoke_profile


def _round():
    deadline = datetime(2026, 11, 4, 10, tzinfo=UTC)
    return {
        "network": "testnet", "status": "locked", "incumbent_hotkey": None,
        "deadline_at": deadline.isoformat(), "baseline_package_sha256": "a" * 64,
        "rules_public": {"policy": {
            "test_profile": "testnet_smoke_v1",
            "schedule": {
                "mode": "custom", "reveal_delay_blocks": 1,
                "gold_close_at": (deadline + timedelta(minutes=30)).isoformat(),
                "publish_at": (deadline + timedelta(minutes=35)).isoformat(),
                "production_activate_at": (deadline + timedelta(minutes=40)).isoformat(),
            },
        }},
    }


def test_smoke_profile_draws_two_papers_from_a_larger_pool(monkeypatch):
    record = _round()
    assert smoke_profile(record).known_answer_count == 1

    class Backend:
        receipt = None

        def get_round(self, _):
            return record

        def list_submissions(self, _):
            return []

        def reveal_sample(self, _, receipt):
            self.receipt = receipt
            return {"status": "revealed"}

    signature = b"s" * 96
    import hashlib
    beacon = VerifiedBeacon(
        5, hashlib.sha256(signature).digest(), signature.hex(),
        DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, 1_800_000_000,
    )
    monkeypatch.setattr("competition.round_controller.beacon_for_round",
                        lambda *_args, **_kwargs: beacon)
    monkeypatch.setattr("competition.round_controller.select_distinct_submissions",
                        lambda *_args, **_kwargs: SimpleNamespace(selected_hotkeys=("miner",)))
    backend = Backend()
    papers = [PaperRecord(f"paper-{i}", f"{i:064x}") for i in range(4)]
    result = reveal_selected_sample(
        "smoke", backend=backend, chain=object(), papers=papers,
        baseline={"package_sha256": "a" * 64},
        package_for_submission=lambda _: b"package", weekly_budget_usd=1,
    )
    assert result["final_paper_count"] == 2
    assert len(backend.receipt["papers"]) == 2
    assert {row["stage"] for row in backend.receipt["papers"]} == {"week_one"}


def test_smoke_profile_rejects_mainnet_and_incumbent_rounds():
    mainnet = {**_round(), "network": "mainnet"}
    with pytest.raises(ValueError, match="test profile"):
        smoke_profile(mainnet)
    later_week = {**_round(), "incumbent_hotkey": "incumbent"}
    with pytest.raises(ValueError, match="test profile"):
        smoke_profile(later_week)
