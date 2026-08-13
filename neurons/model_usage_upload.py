from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKUP_SCHEMA = "claims_model_usage_backup_v2"


def prepare_model_usage_backup(
    path: Path,
    *,
    events: list[dict[str, Any]],
    network: str,
    run_id: str,
    batch_id: str,
) -> dict[str, Any]:
    event_ids_hash = _event_ids_hash(events)
    existing = read_model_usage_backup(path)
    if (
        existing.get("schema") == BACKUP_SCHEMA
        and existing.get("run_id") == run_id
        and existing.get("event_ids_sha256") == event_ids_hash
    ):
        return existing
    payload = {
        "schema": BACKUP_SCHEMA,
        "network": network,
        "run_id": run_id,
        "batch_id": batch_id,
        "expected_event_count": len(events),
        "event_ids_sha256": event_ids_hash,
        "upload": {
            "status": "pending",
            "next_offset": 0,
            "uploaded_event_count": 0,
            "inserted_event_count": 0,
            "stored_event_count": 0,
            "verified": False,
            "error": None,
            "updated_at": _now(),
        },
        "events": events,
    }
    write_model_usage_backup(path, payload)
    return payload


def upload_model_usage_backup(
    path: Path,
    *,
    backend_client: Any,
    chunk_size: int = 1000,
) -> dict[str, Any]:
    payload = read_model_usage_backup(path)
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    if payload.get("schema") != BACKUP_SCHEMA or not events:
        raise ValueError(f"Invalid model usage backup: {path}")
    upload = payload.get("upload") if isinstance(payload.get("upload"), dict) else {}
    start_offset = max(0, min(int(upload.get("next_offset") or 0), len(events)))
    prior_inserted = int(upload.get("inserted_event_count") or 0)

    def checkpoint(progress: dict[str, Any]) -> None:
        current = payload.get("upload") if isinstance(payload.get("upload"), dict) else {}
        cumulative_progress = dict(progress)
        cumulative_progress["inserted_event_count"] = prior_inserted + int(progress.get("inserted_event_count") or 0)
        payload["upload"] = {
            **current,
            **cumulative_progress,
            "status": "uploading",
            "verified": False,
            "error": None,
            "updated_at": _now(),
        }
        write_model_usage_backup(path, payload)

    try:
        result = backend_client.post_model_usage_events(
            events,
            start_offset=start_offset,
            chunk_size=chunk_size,
            on_progress=checkpoint,
        )
    except Exception as exc:
        current = payload.get("upload") if isinstance(payload.get("upload"), dict) else {}
        payload["upload"] = {
            **current,
            "status": "failed",
            "error": str(exc),
            "updated_at": _now(),
        }
        write_model_usage_backup(path, payload)
        raise

    expected = len(events)
    result["inserted_event_count"] = prior_inserted + int(result.get("inserted_event_count") or 0)
    stored = int(result.get("stored_event_count") or 0)
    verified = bool(result.get("verified"))
    complete = bool(result.get("complete")) and (not verified or stored >= expected)
    if verified and not complete:
        result["next_offset"] = 0
    payload["upload"] = {
        **(payload.get("upload") if isinstance(payload.get("upload"), dict) else {}),
        **result,
        "status": "complete" if verified and complete else "uploaded_unverified" if complete else "pending",
        "error": result.get("verification_error"),
        "updated_at": _now(),
    }
    write_model_usage_backup(path, payload)
    if verified and not complete:
        raise RuntimeError(f"Backend stores {stored} of {expected} expected model usage events for {payload.get('run_id')}.")
    return dict(payload["upload"])


def pending_model_usage_backups(root: Path) -> list[Path]:
    if not root.exists():
        return []
    pending: list[Path] = []
    for path in sorted(root.glob("*.json")):
        payload = read_model_usage_backup(path)
        upload = payload.get("upload") if isinstance(payload.get("upload"), dict) else {}
        if payload.get("schema") == BACKUP_SCHEMA and upload.get("status") != "complete":
            pending.append(path)
    return pending


def read_model_usage_backup(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_model_usage_backup(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _event_ids_hash(events: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(str(event.get("usage_event_id") or "").encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
