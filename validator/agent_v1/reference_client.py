from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from miner.agent_v1.runtime.usage import usage_from_cli_process

from .artifact_summary import summarize_agent_artifact_path


logger = logging.getLogger(__name__)


class BronzeRecord(BaseModel):
    bronze_record_id: str
    paper_id: str
    reference_release_id: str
    reference_profile_id: str
    model_runtime_id: str | None = None
    pipeline_version: str | None = None
    source_sha256: str | None = None
    artifact_sha256: str
    source_payload_sha256: str | None = None
    artifact_path: str
    source_payload_path: str | None = None
    artifact: dict[str, Any] | None = None
    source_payload: dict[str, Any] | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReferenceMinerInput(BaseModel):
    paper_id: str
    run_id: str = ""
    batch_id: str = ""
    network: str = "testnet"
    paper_url: str = ""
    source_sha256: str = ""
    input_path: str | None = None
    input_kind: str | None = None
    artifact: dict[str, Any] | None = None


class ReferenceMinerClient(Protocol):
    def get_bronze(self, *, paper_id: str, reference_release_id: str) -> BronzeRecord:
        """Fetch an existing Bronze record."""

    def get_or_create_bronze(self, *, request: ReferenceMinerInput, reference_release_id: str) -> BronzeRecord:
        """Fetch Bronze, or run the private reference miner to create it."""


class LocalReferenceMinerClient:
    """Read Bronze manifests produced by the private reference miner.

    This client is intentionally dumb: it only consumes public artifact paths,
    hashes, and version metadata. The private reference miner remains outside
    the public Claims validator.
    """

    def __init__(self, bronze_root: Path) -> None:
        self.bronze_root = bronze_root

    def get_bronze(self, *, paper_id: str, reference_release_id: str) -> BronzeRecord:
        manifest_path = self._find_manifest(paper_id=paper_id, reference_release_id=reference_release_id)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = BronzeRecord.model_validate(payload)
        artifact_path = Path(record.artifact_path)
        if not artifact_path.is_absolute():
            artifact_path = (manifest_path.parent / artifact_path).resolve()
            record.artifact_path = str(artifact_path)
        if not artifact_path.exists():
            raise FileNotFoundError(f"Bronze artifact does not exist: {artifact_path}")
        record.artifact = _read_json_object(artifact_path)
        if record.source_payload_path:
            source_payload_path = Path(record.source_payload_path)
            if not source_payload_path.is_absolute():
                source_payload_path = (manifest_path.parent / source_payload_path).resolve()
                record.source_payload_path = str(source_payload_path)
            record.source_payload = _read_json_object(source_payload_path)
        return record

    def get_or_create_bronze(self, *, request: ReferenceMinerInput, reference_release_id: str) -> BronzeRecord:
        return self.get_bronze(paper_id=request.paper_id, reference_release_id=reference_release_id)

    def _find_manifest(self, *, paper_id: str, reference_release_id: str) -> Path:
        direct = self.bronze_root / paper_id / "bronze_manifest.json"
        candidates = [direct] if direct.exists() else []
        candidates.extend(sorted(self.bronze_root.glob("**/bronze_manifest.json")))
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("paper_id") == paper_id and payload.get("reference_release_id") == reference_release_id:
                return path
        raise FileNotFoundError(
            f"No Bronze manifest found for paper_id={paper_id!r}, reference_release_id={reference_release_id!r} under {self.bronze_root}"
        )


class LocalCliReferenceMinerClient:
    """Run a private reference-miner CLI when no local Bronze manifest exists."""

    def __init__(
        self,
        *,
        bronze_root: Path,
        command: list[str],
        claims_repo: Path | None = None,
    ) -> None:
        self.local = LocalReferenceMinerClient(bronze_root)
        self.bronze_root = bronze_root
        self.command = command
        self.claims_repo = claims_repo

    def get_bronze(self, *, paper_id: str, reference_release_id: str) -> BronzeRecord:
        return self.local.get_bronze(paper_id=paper_id, reference_release_id=reference_release_id)

    def get_or_create_bronze(self, *, request: ReferenceMinerInput, reference_release_id: str) -> BronzeRecord:
        try:
            return self.get_bronze(paper_id=request.paper_id, reference_release_id=reference_release_id)
        except FileNotFoundError:
            pass
        if not self.command:
            raise FileNotFoundError(f"Bronze missing and no private reference miner command configured for paper_id={request.paper_id!r}")
        input_path, input_kind = self._materialize_input(request)
        output_dir = self.bronze_root / request.paper_id
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            *self.command,
            f"--{input_kind}",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--paper-id",
            request.paper_id,
            "--reference-release-id",
            reference_release_id,
        ]
        if self.claims_repo is not None:
            command.extend(["--claims-repo", str(self.claims_repo)])
        command = _resolve_executable(command)
        logger.info("Running private reference miner command: %s", " ".join(command))
        started = time.time()
        started_at = datetime.now(timezone.utc)
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=3600)
        elapsed = round(time.time() - started, 3)
        if completed.stdout.strip():
            logger.info("private reference miner stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            logger.info("private reference miner stderr: %s", completed.stderr.strip())
        if completed.returncode != 0:
            raise RuntimeError(
                "private reference miner command failed "
                f"with exit={completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        record = self.get_bronze(paper_id=request.paper_id, reference_release_id=reference_release_id)
        runtime_metrics = record.metadata.get("runtime_metrics") if isinstance(record.metadata.get("runtime_metrics"), dict) else {}
        if not _runtime_metrics_has_usage(runtime_metrics):
            model = _reference_model_from_record(record)
            usage = usage_from_cli_process(
                _reference_usage_probe_command(command),
                completed.stdout,
                completed.stderr,
                cwd=output_dir,
                started_at=started_at,
                model=model,
            )
            if _usage_has_value(usage):
                runtime_metrics = _runtime_metrics_from_usage(
                    usage,
                    elapsed_seconds=elapsed,
                    model=model,
                    harness=_reference_harness_from_record(record),
                )
        record.metadata = {
            **record.metadata,
            "generated_for_run_id": request.run_id,
            "generated_for_batch_id": request.batch_id,
        }
        if runtime_metrics:
            record.metadata["runtime_metrics"] = runtime_metrics
        return record

    def _materialize_input(self, request: ReferenceMinerInput) -> tuple[Path, str]:
        if request.input_path and request.input_kind:
            return Path(request.input_path), request.input_kind
        if request.artifact is not None:
            input_dir = self.bronze_root / request.paper_id
            input_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = input_dir / "reference_input_artifact.json"
            artifact_path.write_text(json.dumps(request.artifact, indent=2, ensure_ascii=False), encoding="utf-8")
            return artifact_path, "artifact-json"
        raise ValueError("reference miner generation requires input_path/input_kind or artifact")


class BackendBackedReferenceMinerClient:
    """Fetch Bronze from backend first; optionally delegate creation to a private client."""

    def __init__(self, *, backend: Any, delegate: ReferenceMinerClient | None = None) -> None:
        self.backend = backend
        self.delegate = delegate

    def get_bronze(self, *, paper_id: str, reference_release_id: str) -> BronzeRecord:
        row = self.backend.get_bronze_record(paper_id=paper_id, reference_release_id=reference_release_id)
        return bronze_record_from_backend_row(row)

    def get_or_create_bronze(self, *, request: ReferenceMinerInput, reference_release_id: str) -> BronzeRecord:
        try:
            record = self.get_bronze(paper_id=request.paper_id, reference_release_id=reference_release_id)
        except Exception:
            if self.delegate is None:
                raise
        else:
            if (not _record_has_portable_payload(record) or not isinstance(record.metadata.get("artifact_summary"), dict)) and self.delegate is not None:
                try:
                    local_record = self.delegate.get_or_create_bronze(request=request, reference_release_id=reference_release_id)
                    local_record.metadata = {**record.metadata, **local_record.metadata}
                    record = local_record
                except Exception:
                    if not _record_has_portable_payload(record):
                        raise
            self._post_bronze_record(record=record, request=request)
            return record
        record = self.delegate.get_or_create_bronze(request=request, reference_release_id=reference_release_id)
        self._post_bronze_record(record=record, request=request)
        return record

    def _post_bronze_record(self, *, record: BronzeRecord, request: ReferenceMinerInput) -> None:
        metadata = {
            **record.metadata,
            "pipeline_version": record.pipeline_version,
            "source_sha256": record.source_sha256,
            "source_payload_sha256": record.source_payload_sha256,
            "run_id": request.run_id,
            "batch_id": request.batch_id,
        }
        if not isinstance(metadata.get("artifact_summary"), dict):
            artifact_summary = summarize_agent_artifact_path(record.artifact_path)
            if artifact_summary:
                metadata["artifact_summary"] = artifact_summary
        artifact = record.artifact if isinstance(record.artifact, dict) else _read_json_object(record.artifact_path)
        source_payload = (
            record.source_payload
            if isinstance(record.source_payload, dict)
            else _read_json_object(record.source_payload_path)
        )
        self.backend.post_bronze_record(
            {
                "bronze_record_id": record.bronze_record_id,
                "network": request.network,
                "run_id": request.run_id or record.metadata.get("run_id") or "reference_miner",
                "batch_id": request.batch_id or record.metadata.get("batch_id") or "reference_miner",
                "paper_id": record.paper_id,
                "reference_release_id": record.reference_release_id,
                "reference_profile_id": record.reference_profile_id,
                "model_runtime_id": record.model_runtime_id,
                "artifact_hash": record.artifact_sha256,
                "artifact_uri": f"claims-api:/bronze-records/{record.bronze_record_id}/artifact",
                "source_payload_uri": (
                    f"claims-api:/bronze-records/{record.bronze_record_id}/source-payload"
                    if isinstance(source_payload, dict)
                    else None
                ),
                "artifact": artifact,
                "source_payload": source_payload,
                "metadata": metadata,
                "status": "created",
            }
        )


def bronze_record_from_backend_row(row: dict[str, Any]) -> BronzeRecord:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return BronzeRecord(
        bronze_record_id=str(row["bronze_record_id"]),
        paper_id=str(row["paper_id"]),
        reference_release_id=str(row["reference_release_id"]),
        reference_profile_id=str(row.get("reference_profile_id") or ""),
        model_runtime_id=row.get("model_runtime_id"),
        pipeline_version=metadata.get("pipeline_version"),
        source_sha256=metadata.get("source_sha256"),
        artifact_sha256=str(row.get("artifact_hash") or ""),
        source_payload_sha256=metadata.get("source_payload_sha256"),
        artifact_path=str(row.get("artifact_uri") or ""),
        source_payload_path=str(row.get("source_payload_uri") or "") or None,
        artifact=row.get("artifact") if isinstance(row.get("artifact"), dict) else None,
        source_payload=row.get("source_payload") if isinstance(row.get("source_payload"), dict) else None,
        created_at=row.get("created_at"),
        metadata=metadata,
    )


def _record_has_portable_payload(record: BronzeRecord) -> bool:
    return (
        isinstance(record.artifact, dict)
        and bool(record.artifact)
        and isinstance(record.source_payload, dict)
        and bool(record.source_payload)
    )


def _read_json_object(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_executable(command: list[str]) -> list[str]:
    if not command:
        return command
    executable = command[0]
    executable_name = Path(executable).name
    if executable_name in {"python", "python3"}:
        return [sys.executable, *command[1:]]
    executable_path = Path(executable)
    if executable_path.is_absolute() or executable_path.exists():
        return command
    resolved = shutil.which(executable)
    if resolved:
        return [resolved, *command[1:]]
    return command


def _reference_usage_probe_command(command: list[str]) -> list[str]:
    for env_name in ("CLAIMS_REFERENCE_MINER_INNER_COMMAND", "CLAIMS_AGENT_INNER_COMMAND"):
        env_command = os.getenv(env_name, "").strip()
        if not env_command:
            continue
        try:
            parsed = shlex.split(env_command)
        except ValueError:
            parsed = env_command.split()
        if parsed:
            return parsed
    return command


def _reference_model_from_record(record: BronzeRecord) -> str:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    runtime_metrics = metadata.get("runtime_metrics") if isinstance(metadata.get("runtime_metrics"), dict) else {}
    models = runtime_metrics.get("models") if isinstance(runtime_metrics.get("models"), list) else []
    return (
        str(metadata.get("model") or "")
        or str(getattr(record, "model_runtime_id", "") or "")
        or (str(models[0]) if len(models) == 1 else "")
        or os.getenv("CLAIMS_REFERENCE_MINER_MODEL", "")
        or os.getenv("SUBNET_CLAIMS_REFERENCE_MODEL", "")
        or os.getenv("OPENROUTER_MODEL", "")
    )


def _reference_harness_from_record(record: BronzeRecord) -> str:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    runtime_metrics = metadata.get("runtime_metrics") if isinstance(metadata.get("runtime_metrics"), dict) else {}
    return str(metadata.get("harness") or runtime_metrics.get("harness") or metadata.get("runtime") or "unknown")


def _runtime_metrics_has_usage(metrics: dict[str, Any]) -> bool:
    token_usage = metrics.get("token_usage") if isinstance(metrics.get("token_usage"), dict) else {}
    return any(
        value is not None
        for value in (
            metrics.get("cost_usd"),
            token_usage.get("prompt_tokens"),
            token_usage.get("completion_tokens"),
            token_usage.get("total_tokens"),
        )
    )


def _usage_has_value(usage: dict[str, Any]) -> bool:
    return any(
        usage.get(key) is not None
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "cost_usd",
        )
    )


def _runtime_metrics_from_usage(
    usage: dict[str, Any],
    *,
    elapsed_seconds: float,
    model: str,
    harness: str,
) -> dict[str, Any]:
    return {
        "elapsed_seconds": elapsed_seconds,
        "attempt_count": 1,
        "harness": harness,
        "harnesses": [harness] if harness and harness != "unknown" else [],
        "models": [model] if model else [],
        "token_usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "cost_usd": usage.get("cost_usd"),
        "cost_kind": usage.get("cost_kind") or ("estimated" if usage.get("cost_usd") is not None else "unavailable"),
        "usage_source": usage.get("source") or "unavailable",
    }
