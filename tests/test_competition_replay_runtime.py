"""Existing worker inputs can bind to local replay writes and remote GETs."""

import io
import json

import pytest

from competition.paper_inputs import CompetitionPaperInputs
from competition.replay_inputs import CompetitionReplayInputs
from competition.replay_runtime import ReplayRuntimeBackend
from competition.replay_store import ReplayStore
from competition.sample_release import PaperRecord
from test_competition_replay_store import _inputs


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
    with pytest.raises(ValueError, match="stored frozen inputs"):
        ReplayRuntimeBackend(
            store, reader, changed, (PaperRecord("paper-1", "e" * 64),),
        )
