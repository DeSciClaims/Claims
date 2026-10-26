"""Existing worker inputs can bind to local replay writes and remote GETs."""

import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from competition.paper_inputs import CompetitionPaperInputs
from competition.replay_inputs import CompetitionReplayInputs
from competition.replay_runtime import ReplayRuntimeBackend, initialize_replay_runtime
from competition.replay_store import ReplayStore
from competition.sample_release import PaperRecord
from test_competition_replay_store import _inputs
from test_competition_replay_plan import (
    Archive, BASELINE, BEACON, PAPER_POOL, PUBLIC_KEY, _package, _snapshot,
)


def test_replay_runtime_uses_local_jobs_and_read_only_bronze(tmp_path):
    snapshot, plan = _inputs()
    store = ReplayStore(tmp_path / "replay.sqlite")
    store.initialize(snapshot, plan)
    requests = []

    def respond(request, *, timeout):
        requests.append(request)
        assert request.get_method() == "GET" and timeout == 30
        return io.BytesIO(json.dumps({
            "paper_id": "paper-1", "source_sha256": "e" * 64,
            "source_url": "https://papers.example/paper-1.pdf",
            "bronze_record_id": "bronze-1", "full_source_id": "full-1",
            "artifact": {"paper": {"paper_id": "paper-1"}},
            "source_context_by_span_id": {"span-1": "Evidence"},
        }).encode())

    reader = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    backend = ReplayRuntimeBackend(
        store, reader, snapshot, (PaperRecord("paper-1", "e" * 64),),
    )
    papers = CompetitionPaperInputs(
        backend, "round-1", "release-1",
        expected_pool_sha256=snapshot["paper_pool_sha256"],
    )
    assert papers.pdf_source_for_paper("week_one", "paper-1") == (
        "https://papers.example/paper-1.pdf", "e" * 64,
    )
    assert backend.claim_job("round-1", worker_id="replay-worker", lease_seconds=60)["hotkey"] == "reference"
    assert len(requests) == 1
    with pytest.raises(ValueError, match="reference release"):
        backend.list_paper_pool("round-1", reference_release_id="changed")


def test_replay_runtime_rejects_changed_local_configuration(tmp_path):
    snapshot, plan = _inputs()
    store = ReplayStore(tmp_path / "replay.sqlite")
    store.initialize(snapshot, plan)
    reader = CompetitionReplayInputs("https://backend.example", "replay-only")
    changed = {**snapshot, "network": "mainnet"}
    with pytest.raises(ValueError, match="identity differs from hashed rules"):
        ReplayRuntimeBackend(
            store, reader, changed, (PaperRecord("paper-1", "e" * 64),),
        )


def test_local_reference_rerun_reads_original_stage_bronze():
    snapshot, _ = _inputs()
    snapshot["sample"][0]["stage"] = "screen"
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        assert timeout == 30
        return io.BytesIO(json.dumps({
            "paper_id": "paper-1", "source_sha256": "e" * 64,
            "source_url": "https://papers.example/paper-1.pdf",
            "artifact": {"paper": {"paper_id": "paper-1"}},
        }).encode())

    class LocalStore:
        def get_round(self, round_id):
            return {"configuration_sha256": snapshot["configuration_sha256"],
                    "network": snapshot["network"]}

        def list_sample(self, round_id):
            return [*snapshot["sample"],
                    {**snapshot["sample"][0], "stage": "rerun"}]

        def list_stages(self, round_id):
            return [{"stage": "screen"},
                    {"stage": "rerun", "source_stage": "screen"}]

    backend = ReplayRuntimeBackend(
        LocalStore(),
        CompetitionReplayInputs("https://backend.example", "replay-only", open_request=respond),
        snapshot, (PaperRecord("paper-1", "e" * 64),),
    )
    assert backend.get_bronze_source(
        "round-1", stage="rerun", paper_id="paper-1",
        reference_release_id="release-1",
    )["source_sha256"] == "e" * 64
    assert requests == [
        "https://backend.example/internal/competition/replay/rounds/round-1/bronze/screen/paper-1",
    ]


def test_replay_bootstrap_verifies_inputs_before_writing_local_jobs(tmp_path):
    snapshot = _snapshot()
    snapshot["reference_release_id"] = "release-1"
    snapshot["reference_package_sha256"] = snapshot["baseline_package_sha256"]

    class Inputs:
        def snapshot(self, round_id):
            assert round_id == "week-1"
            return snapshot

        def paper_pool(self, round_id, *, reference_release_id, expected_sha256):
            assert (round_id, reference_release_id, expected_sha256) == (
                "week-1", "release-1", snapshot["paper_pool_sha256"],
            )
            return PAPER_POOL

    class ClockArchive(Archive):
        def get_current_block(self):
            return 700

        def get_timestamp(self, block):
            assert block == 700
            return datetime(2026, 10, 30, tzinfo=UTC)

    store = ReplayStore(tmp_path / "replay.sqlite")
    args = {
        "store": store, "inputs": Inputs(), "archive": ClockArchive(),
        "netuid": 111,
        "storage": SimpleNamespace(package_for_submission=_package),
        "baseline_upload": {
            "hotkey": "reference", "team_id": "claims-reference",
            "package_sha256": snapshot["baseline_package_sha256"],
            "package_size_bytes": len(BASELINE),
        },
        "hotkey_public_key": lambda _: PUBLIC_KEY,
        "fetch_beacon": lambda **_: BEACON,
    }
    backend = initialize_replay_runtime("week-1", **args)
    assert backend.list_stages("week-1")[0]["stage"] == "week_one"
    assert len(backend.list_paper_jobs("week-1", "week_one", "paper-1")) == 3
    assert initialize_replay_runtime("week-1", **args).store is store
