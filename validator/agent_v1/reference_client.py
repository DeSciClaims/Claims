from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


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
        if record.source_payload_path:
            source_payload_path = Path(record.source_payload_path)
            if not source_payload_path.is_absolute():
                source_payload_path = (manifest_path.parent / source_payload_path).resolve()
                record.source_payload_path = str(source_payload_path)
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
        logger.info("Running private reference miner command: %s", " ".join(command))
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=3600)
        if completed.stdout.strip():
            logger.info("private reference miner stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            logger.info("private reference miner stderr: %s", completed.stderr.strip())
        if completed.returncode != 0:
            raise RuntimeError(
                "private reference miner command failed "
                f"with exit={completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        return self.get_bronze(paper_id=request.paper_id, reference_release_id=reference_release_id)

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
            return self.get_bronze(paper_id=request.paper_id, reference_release_id=reference_release_id)
        except Exception:
            if self.delegate is None:
                raise
        record = self.delegate.get_or_create_bronze(request=request, reference_release_id=reference_release_id)
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
                "artifact_uri": record.artifact_path,
                "source_payload_uri": record.source_payload_path,
                "metadata": {
                    **record.metadata,
                    "pipeline_version": record.pipeline_version,
                    "source_sha256": record.source_sha256,
                    "source_payload_sha256": record.source_payload_sha256,
                },
                "status": "created",
            }
        )
        return record


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
        created_at=row.get("created_at"),
        metadata=metadata,
    )
