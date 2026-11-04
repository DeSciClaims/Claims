import hashlib

from competition.round_controller import (
    _admitted_duplicate_selection,
    reveal_selected_sample,
    start_revealed_round,
)
from competition.duplicates import duplicate_verdicts
from competition.drand import DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, VerifiedBeacon
from competition.publication import canonical_sha256
from competition.sample_release import PaperRecord
from competition.stage_controller import start_initial_stage, advance_ready_stage
from test_competition_stage_controller import Backend


def entry(name, *, held=False, body=None):
    return {
        "hotkey": name,
        "team_id": "team-" + name,
        "package_sha256": hashlib.sha256(body or name.encode()).hexdigest(),
        "qualified": True,
        "status": "accepted",
        "commit_block": 1,
        "funding_eligible": not held,
    }


def test_launch_skips_only_pending_entry_without_accusing_it_of_copying():
    record = {"incumbent_hotkey": None}
    rows = [entry("ready"), entry("pending", held=True)]
    result = _admitted_duplicate_selection(record, rows, seed=b"a" * 32)
    assert result.selected_hotkeys == ("ready",)
    assert result.held_hotkeys == ("pending",)
    assert duplicate_verdicts(result, seed=b"a" * 32) == []


def test_pending_representative_cannot_eliminate_an_eligible_duplicate_group_mate():
    rows = [entry("ready", body=b"one"), entry("pending", held=True, body=b"one")]
    result = _admitted_duplicate_selection(
        {"incumbent_hotkey": None}, rows, seed=b"a" * 32
    )
    assert result.selected_hotkeys == ("ready",)
    assert result.excluded_hotkeys == () and result.held_hotkeys == ("pending",)
    assert duplicate_verdicts(result, seed=b"a" * 32) == []


def test_pending_entries_can_participate_in_prescreen():
    rows = [entry("miner" + str(i), held=i == 0) for i in range(13)]
    result = _admitted_duplicate_selection(
        {"incumbent_hotkey": "incumbent"}, rows, seed=b"a" * 32
    )
    assert len(result.selected_hotkeys) == 13 and result.held_hotkeys == ()


def prepared_prescreen(*, all_held=False):
    backend = Backend()
    backend.sample = [
        {"stage": s, "paper_id": "paper-" + s} for s in ["prescreen", "screen", "final"]
    ]
    rows = [entry("miner" + str(i), held=all_held or i == 0) for i in range(13)]
    backend.submissions = rows
    start_initial_stage(
        "round",
        backend=backend,
        baseline={
            "hotkey": "incumbent",
            "package_sha256": "a" * 64,
            "team_id": "king-team",
        },
        challengers=rows,
    )
    for index, row in enumerate(rows):
        backend.pairs.append(
            {
                "stage": "prescreen",
                "paper_id": "paper-prescreen",
                "challenger_hotkey": row["hotkey"],
                "challenger_package_sha256": row["package_sha256"],
                "baseline_hotkey": "incumbent",
                "baseline_package_sha256": "a" * 64,
                "challenger_score": 1 - index / 100,
                "baseline_score": 0,
                "infrastructure_fault": False,
            }
        )
    return backend


def test_promotion_omits_pending_entry_even_when_it_had_the_highest_prescreen_score():
    backend = prepared_prescreen()
    result = advance_ready_stage("round", backend=backend)
    assert result["stage"] == "screen" and len(result["selected_hotkeys"]) == 12
    assert "miner0" not in result["selected_hotkeys"]


def test_all_pending_prescreen_uses_backend_owned_no_contest_path():
    backend = prepared_prescreen(all_held=True)
    calls = []

    def close(rid, suffix, payload):
        calls.append((rid, suffix, payload))
        return {"round_id": rid, "status": "no_contest"}

    backend.runtime_request = close
    assert advance_ready_stage("round", backend=backend)["status"] == "no_contest"
    assert calls == [("round", "/funding/no-eligible-entries", {})]
    assert len(backend.ledger) == 1


def test_only_pending_challenger_and_incumbent_copy_close_after_duplicate_receipt(
    monkeypatch,
):
    # The ready entry is a copy of the baseline, so only the held challenger
    # would otherwise be selected. Do not leave the round waiting for entrants.
    baseline = {
        "hotkey": "incumbent",
        "team_id": "king-team",
        "package_sha256": hashlib.sha256(b"baseline").hexdigest(),
    }
    signature = b"a" * 96
    beacon = VerifiedBeacon(
        123,
        hashlib.sha256(signature).digest(),
        signature.hex(),
        DEFAULT_CHAIN_HASH,
        DEFAULT_PUBLIC_KEY,
        1_800_000_030,
    )
    monkeypatch.setattr(
        "competition.round_controller.beacon_for_round", lambda *_, **__: beacon
    )

    class RoundBackend:
        record = {
            "status": "locked",
            "incumbent_hotkey": "incumbent",
            "baseline_package_sha256": baseline["package_sha256"],
        }
        submissions = [entry("copy", body=b"baseline"), entry("pending", held=True)]

        def get_round(self, _):
            return self.record

        def list_submissions(self, _):
            return self.submissions

        def list_stages(self, _):
            return []

        def reveal_sample(self, _, receipt):
            self.record.update(
                status="revealed",
                drand_seed=beacon.seed.hex(),
                duplicate_review_links=[],
                duplicate_review_sha256=canonical_sha256([]),
            )
            self.sample = receipt["papers"]
            return {"status": "revealed"}

        def list_sample(self, _):
            return self.sample

        def mark_duplicates(self, _, verdicts):
            assert len(verdicts) == 1 and verdicts[0]["hotkey"] == "copy"
            self.submissions[0]["status"] = "duplicate"

        def runtime_request(self, _, suffix, payload):
            assert suffix == "/funding/no-eligible-entries" and payload == {}
            assert self.submissions[0]["status"] == "duplicate"
            return {"status": "no_contest"}

    backend = RoundBackend()
    package = lambda row: b"baseline" if row["hotkey"] == "copy" else b"pending"
    papers = [
        PaperRecord(str(i), hashlib.sha256(str(i).encode()).hexdigest())
        for i in range(500)
    ]
    assert (
        reveal_selected_sample(
            "round",
            backend=backend,
            chain=object(),
            papers=papers,
            baseline=baseline,
            package_for_submission=package,
            incumbent_package=b"baseline",
            weekly_budget_usd=295,
        )["status"]
        == "revealed"
    )
    assert (
        start_revealed_round(
            "round",
            backend=backend,
            baseline=baseline,
            package_for_submission=package,
            incumbent_package=b"baseline",
        )["status"]
        == "no_contest"
    )
