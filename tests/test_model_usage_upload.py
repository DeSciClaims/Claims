from __future__ import annotations

import pytest

from neurons.model_usage_upload import (
    pending_model_usage_backups,
    prepare_model_usage_backup,
    read_model_usage_backup,
    upload_model_usage_backup,
    write_model_usage_backup,
)
from validator.agent_v1.model_usage import ModelUsageCollector


def test_model_usage_backup_resumes_from_last_confirmed_chunk(tmp_path) -> None:
    backup_path = tmp_path / "model_usage" / "run_usage.json"
    events = [
        {"usage_event_id": f"usage_{index}", "run_id": "run_usage", "batch_id": "batch_usage"}
        for index in range(3)
    ]
    prepare_model_usage_backup(
        backup_path,
        events=events,
        network="testnet",
        run_id="run_usage",
        batch_id="batch_usage",
    )

    class FailingClient:
        def post_model_usage_events(self, rows, *, start_offset, chunk_size, on_progress):
            assert start_offset == 0
            on_progress(
                {
                    "expected_event_count": 3,
                    "uploaded_event_count": 2,
                    "inserted_event_count": 2,
                    "next_offset": 2,
                }
            )
            raise RuntimeError("temporary backend failure")

    with pytest.raises(RuntimeError, match="temporary backend failure"):
        upload_model_usage_backup(backup_path, backend_client=FailingClient(), chunk_size=2)

    failed = read_model_usage_backup(backup_path)
    assert failed["upload"]["status"] == "failed"
    assert failed["upload"]["next_offset"] == 2
    assert pending_model_usage_backups(backup_path.parent) == [backup_path]

    class ResumingClient:
        def post_model_usage_events(self, rows, *, start_offset, chunk_size, on_progress):
            assert start_offset == 2
            on_progress(
                {
                    "expected_event_count": 3,
                    "uploaded_event_count": 3,
                    "inserted_event_count": 1,
                    "next_offset": 3,
                }
            )
            return {
                "expected_event_count": 3,
                "uploaded_event_count": 3,
                "inserted_event_count": 1,
                "stored_event_count": 3,
                "next_offset": 3,
                "verified": True,
                "complete": True,
                "verification_error": None,
            }

    summary = upload_model_usage_backup(backup_path, backend_client=ResumingClient(), chunk_size=2)

    assert summary["status"] == "complete"
    assert summary["expected_event_count"] == 3
    assert summary["inserted_event_count"] == 3
    assert summary["stored_event_count"] == 3
    assert pending_model_usage_backups(backup_path.parent) == []


def test_model_usage_backup_preserves_uploaded_prefix_when_events_are_appended(tmp_path) -> None:
    backup_path = tmp_path / "model_usage" / "run_append.json"
    initial_events = [
        {"usage_event_id": f"usage_{index}", "run_id": "run_append", "batch_id": "batch_append"}
        for index in range(2)
    ]
    prepare_model_usage_backup(
        backup_path,
        events=initial_events,
        network="testnet",
        run_id="run_append",
        batch_id="batch_append",
    )
    payload = read_model_usage_backup(backup_path)
    payload["upload"].update(
        {
            "status": "complete",
            "next_offset": 2,
            "uploaded_event_count": 2,
            "inserted_event_count": 2,
            "stored_event_count": 2,
        }
    )
    write_model_usage_backup(backup_path, payload)

    appended = [
        *initial_events,
        {"usage_event_id": "usage_2", "run_id": "run_append", "batch_id": "batch_append"},
    ]
    updated = prepare_model_usage_backup(
        backup_path,
        events=appended,
        network="testnet",
        run_id="run_append",
        batch_id="batch_append",
    )

    assert updated["expected_event_count"] == 3
    assert updated["upload"]["next_offset"] == 2
    assert updated["upload"]["inserted_event_count"] == 2


def test_model_usage_checkpoint_failure_does_not_fail_recording(caplog) -> None:
    def fail_checkpoint(_events):
        raise OSError("disk unavailable")

    collector = ModelUsageCollector(
        network="testnet",
        run_id="run_usage",
        batch_id="batch_usage",
        checkpoint_sink=fail_checkpoint,
        checkpoint_every=1,
    )

    collector.record({"stage_key": "adjudication", "usage": {}})

    assert len(collector.snapshot()) == 1
    assert "disk unavailable" in caplog.text
