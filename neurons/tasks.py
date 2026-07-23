from __future__ import annotations

import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


PROTOCOL_VERSION = "claims.v0"
SCHEMA_VERSION = "miner.v0.section_context_compat"
DEFAULT_MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024


@dataclass(frozen=True)
class ClaimsPaperTask:
    paper_id: str = ""
    paper_url: str = ""
    source_sha256: str = ""
    title: str = ""
    topics: tuple[str, ...] = ()
    release_id: str = ""
    artifact: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClaimsPaperTask":
        return cls(
            paper_id=str(payload.get("paper_id") or "").strip(),
            paper_url=str(payload.get("paper_url") or payload.get("source_url") or "").strip(),
            source_sha256=str(payload.get("source_sha256") or "").strip().lower(),
            title=str(payload.get("title") or "").strip(),
            topics=tuple(str(item).strip() for item in payload.get("topics", []) if str(item).strip()),
            release_id=str(payload.get("release_id") or "").strip(),
            artifact=payload.get("artifact") if isinstance(payload.get("artifact"), dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "paper_url": self.paper_url,
            "source_url": self.paper_url,
            "source_sha256": self.source_sha256,
            "title": self.title,
            "topics": list(self.topics),
            "release_id": self.release_id,
            "artifact": self.artifact,
        }


@dataclass(frozen=True)
class ClaimsTask:
    task_id: str
    paper_url: str = ""
    paper_id: str = ""
    source_sha256: str = ""
    artifact: dict[str, Any] | None = None
    batch_id: str = ""
    selection_seed: str = ""
    task_version: str = "claims_task_v0"
    scoring_version: str = "agent_v1_pass4_deterministic_v0"
    task_type: str = "agent_v1_claim_extraction"
    network: str = "testnet"
    netuid: int | None = None
    papers: tuple[ClaimsPaperTask, ...] = ()
    protocol_version: str = PROTOCOL_VERSION
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fallback_task_id: str = "claims_task") -> "ClaimsTask":
        artifact = payload.get("artifact")
        paper = artifact.get("paper") if isinstance(artifact, dict) else None
        papers_payload = payload.get("papers") if isinstance(payload.get("papers"), list) else []
        papers = tuple(ClaimsPaperTask.from_dict(item) for item in papers_payload if isinstance(item, dict))
        paper_id = str(payload.get("paper_id") or (paper or {}).get("paper_id") or "").strip()
        task_id = str(payload.get("task_id") or paper_id or fallback_task_id).strip()
        return cls(
            task_id=safe_task_id(task_id),
            paper_url=str(payload.get("paper_url") or "").strip(),
            paper_id=paper_id,
            source_sha256=str(payload.get("source_sha256") or "").strip().lower(),
            artifact=artifact if isinstance(artifact, dict) else None,
            batch_id=safe_task_id(str(payload.get("batch_id") or "").strip()) if payload.get("batch_id") else "",
            selection_seed=str(payload.get("selection_seed") or "").strip(),
            task_version=str(payload.get("task_version") or "claims_task_v0").strip(),
            scoring_version=str(payload.get("scoring_version") or "agent_v1_pass4_deterministic_v0").strip(),
            task_type=str(payload.get("task_type") or "agent_v1_claim_extraction").strip(),
            network=str(payload.get("network") or "testnet").strip(),
            netuid=int(payload["netuid"]) if payload.get("netuid") is not None else None,
            papers=papers,
            protocol_version=str(payload.get("protocol_version") or PROTOCOL_VERSION).strip(),
            schema_version=str(payload.get("schema_version") or SCHEMA_VERSION).strip(),
        )

    def paper_tasks(self) -> tuple[ClaimsPaperTask, ...]:
        if self.papers:
            return self.papers
        return (
            ClaimsPaperTask(
                paper_id=self.paper_id,
                paper_url=self.paper_url,
                source_sha256=self.source_sha256,
                artifact=self.artifact,
            ),
        )

    def to_synapse_kwargs(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "batch_id": self.batch_id,
            "selection_seed": self.selection_seed,
            "task_version": self.task_version,
            "scoring_version": self.scoring_version,
            "task_type": self.task_type,
            "network": self.network,
            "netuid": self.netuid,
            "papers": [paper.to_dict() for paper in self.papers],
            "paper_id": self.paper_id,
            "paper_url": self.paper_url,
            "source_sha256": self.source_sha256,
            "artifact": self.artifact,
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DownloadedPDF:
    path: Path
    sha256: str
    content_type: str
    size_bytes: int


class PDFDownloadError(RuntimeError):
    pass


def safe_task_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
    return cleaned or "claims_task"


def task_cache_key(task: ClaimsTask, *, miner_version: str, model_config: str = "") -> str:
    payload = {
        "paper_url": normalize_url(task.paper_url) if task.paper_url else "",
        "source_sha256": task.source_sha256,
        "artifact": _stable_artifact_fingerprint(task.artifact),
        "batch_id": task.batch_id,
        "papers": [
            {
                "paper_id": paper.paper_id,
                "paper_url": normalize_url(paper.paper_url) if paper.paper_url else "",
                "source_sha256": paper.source_sha256,
                "artifact": _stable_artifact_fingerprint(paper.artifact),
            }
            for paper in task.papers
        ],
        "miner_version": miner_version,
        "model_config": model_config,
        "protocol_version": task.protocol_version,
        "schema_version": task.schema_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PDFDownloadError(f"Unsupported paper URL: {url}")
    return parsed.geturl()


def download_pdf(
    url: str,
    *,
    output_dir: Path,
    expected_sha256: str = "",
    max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    timeout_s: float = 60.0,
) -> DownloadedPDF:
    normalized = normalize_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    request = Request(normalized, headers={"User-Agent": "claims-subnet/0.1"})
    digest = hashlib.sha256()
    size = 0
    content_type = ""
    suffix = _suffix_from_url(normalized)
    tmp_path = output_dir / f"download-{int(time.time() * 1000)}.tmp"
    try:
        with urlopen(request, timeout=timeout_s) as response, tmp_path.open("wb") as handle:
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > max_bytes:
                raise PDFDownloadError(f"PDF download is too large: {declared_length} bytes")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise PDFDownloadError(f"PDF download exceeded {max_bytes} bytes")
                digest.update(chunk)
                handle.write(chunk)
    except (OSError, URLError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise PDFDownloadError(f"Failed to download PDF URL: {exc}") from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    sha256 = digest.hexdigest()
    if expected_sha256 and sha256.lower() != expected_sha256.lower():
        tmp_path.unlink(missing_ok=True)
        raise PDFDownloadError(f"Downloaded PDF hash mismatch: expected {expected_sha256}, got {sha256}")
    if content_type and content_type not in {"application/pdf", "application/octet-stream"} and suffix != ".pdf":
        tmp_path.unlink(missing_ok=True)
        raise PDFDownloadError(f"Downloaded URL does not look like a PDF: content-type={content_type}")

    final_path = output_dir / f"{sha256}{suffix}"
    if final_path.exists():
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.replace(final_path)
    return DownloadedPDF(path=final_path, sha256=sha256, content_type=content_type, size_bytes=size)


def load_task_manifest(path: Path) -> list[ClaimsTask]:
    tasks: list[ClaimsTask] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"Task manifest line {line_no} must be a JSON object")
        tasks.append(ClaimsTask.from_dict(payload, fallback_task_id=f"task_{line_no}"))
    if not tasks:
        raise ValueError(f"Task manifest contains no tasks: {path}")
    return tasks


def _stable_artifact_fingerprint(artifact: dict[str, Any] | None) -> str:
    if artifact is None:
        return ""
    return hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _suffix_from_url(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return ".pdf"
    guessed = mimetypes.guess_type(path)[0]
    return ".pdf" if guessed == "application/pdf" else ".pdf"
