"""Trusted one-paper job lifecycle around a disposable miner worker VM."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from .bifrost import PaperKey
from miner.agent_v1.artifact_models import Artifact
from miner.agent_v1.artifact_validator import validate_agent_artifact


@dataclass(frozen=True)
class ExecutionOutput:
    status: str
    artifact: dict[str, Any] | None
    log_bytes: bytes
    detail: str = ""


class InferenceKeys(Protocol):
    def create_paper_key(
        self, *, round_id: str, paper_id: str, hotkey: str,
        allowed_models: tuple[str, ...], token_limit: int,
        dollar_limit: float, expires_at: datetime,
    ) -> PaperKey: ...
    def check_budget(self, key: PaperKey) -> tuple[int, bool]: ...
    def revoke(self, key: PaperKey) -> None: ...


class DisposableExecution(Protocol):
    def run(
        self, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput: ...


class PrivateLogs(Protocol):
    def put(self, key: str, content: bytes) -> None: ...


class JobBackend(Protocol):
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]: ...
    def complete_job(
        self, job_id: str, *, worker_id: str, lease_token: str,
        result: dict[str, Any], log_object_key: str | None,
    ) -> dict[str, Any]: ...


def paper_token_cap(
    job: dict[str, Any], *, backend: JobBackend, published_ceiling: int,
) -> tuple[int, str]:
    """Use 1.5× this paper's successful baseline usage, bounded by the rules.

    A failed baseline has no reliable measured usage. In that case the frozen
    published ceiling applies and the separate baseline-failure rule decides
    whether the paper or week remains valid.
    """
    if isinstance(published_ceiling, bool) or not isinstance(published_ceiling, int) or published_ceiling < 1:
        raise ValueError("published token ceiling must be positive")
    stage = str(job["stage"])
    receipt = next((row for row in backend.list_stages(job["round_id"])
                    if row["stage"] == stage), None)
    if receipt is None:
        raise ValueError("claimed job has no frozen stage")
    entries = receipt["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    entrant = next((row for row in entries if row["hotkey"] == job["hotkey"]), None)
    if (len(baseline) != 1 or entrant is None
        or entrant["package_sha256"] != job["package_sha256"]):
        raise ValueError("claimed job differs from frozen stage entrants")
    if job["hotkey"] == baseline[0]["hotkey"]:
        return published_ceiling, "published_baseline_ceiling"
    jobs = backend.list_paper_jobs(job["round_id"], stage, job["paper_id"])
    baseline_job = next((row for row in jobs if row["hotkey"] == baseline[0]["hotkey"]), None)
    if baseline_job is None or baseline_job["status"] != "completed":
        raise ValueError("baseline must finish before challenger inference")
    if baseline_job["package_sha256"] != baseline[0]["package_sha256"]:
        raise ValueError("baseline job package differs from frozen stage")
    result = baseline_job.get("result_private") or {}
    usage = result.get("prompt_and_completion_tokens")
    if result.get("status") != "completed" or not isinstance(usage, int) or isinstance(usage, bool) or usage < 1:
        return published_ceiling, "published_ceiling_baseline_fault"
    return min(published_ceiling, max(1, usage * 3 // 2)), "baseline_usage_1_5x"


def run_claimed_job(
    job: dict[str, Any], *, backend: JobBackend,
    worker_id: str, network: str,
    fetch_package: Callable[[dict[str, Any]], bytes],
    fetch_pdf: Callable[[dict[str, Any]], bytes],
    expected_pdf_sha256: str,
    keys: InferenceKeys, execution: DisposableExecution,
    logs: PrivateLogs, inference_base_url: str,
    allowed_models: tuple[str, ...], published_token_ceiling: int,
    dollar_limit: float, key_lifetime_seconds: int = 2400,
    max_output_bytes: int = 10_000_000,
) -> dict[str, Any]:
    """Verify inputs, execute once, account for tokens, revoke, then persist.

    Package installation and execution happen only in ``execution.run`` on a
    disposable VM. The caller supplies trusted Hippius fetch and log adapters.
    A failure before durable completion leaves the lease to expire and retry.
    """
    required = ("job_id", "round_id", "hotkey", "package_sha256", "stage", "paper_id", "lease_token")
    if any(not job.get(field) for field in required):
        raise ValueError("claimed job lacks immutable identity or lease token")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", str(job["round_id"]))
        or not re.fullmatch(r"[A-Za-z0-9]{20,80}", str(job["hotkey"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(job["job_id"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(job["package_sha256"]))
        or not re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha256)):
        raise ValueError("claimed job hash or storage path component is invalid")
    if network not in {"testnet", "mainnet"} or not worker_id:
        raise ValueError("worker identity or network is invalid")
    if (not 1 <= key_lifetime_seconds <= 7200 or published_token_ceiling < 1
        or dollar_limit <= 0 or max_output_bytes < 1):
        raise ValueError("paper key budget or lifetime is invalid")
    package = fetch_package(job)
    if hashlib.sha256(package).hexdigest() != job["package_sha256"]:
        raise ValueError("sealed package differs from claimed job hash")
    pdf = fetch_pdf(job)
    if hashlib.sha256(pdf).hexdigest() != expected_pdf_sha256:
        raise ValueError("paper PDF differs from released sample hash")
    token_limit, cap_source = paper_token_cap(
        job, backend=backend, published_ceiling=published_token_ceiling,
    )
    key = keys.create_paper_key(
        round_id=job["round_id"], paper_id=job["paper_id"], hotkey=job["hotkey"],
        allowed_models=allowed_models, token_limit=token_limit,
        dollar_limit=dollar_limit,
        expires_at=datetime.now(UTC) + timedelta(seconds=key_lifetime_seconds),
    )
    try:
        try:
            output = execution.run(
                job=job, package=package, pdf=pdf,
                inference_base_url=inference_base_url, paper_key=key.value,
            )
        except Exception as exc:
            output = ExecutionOutput("infrastructure_fault", None,
                                     f"worker transport failed: {type(exc).__name__}".encode())
        used, over_cap = keys.check_budget(key)
    finally:
        keys.revoke(key)
    if used < 0:
        raise ValueError("gateway returned negative token usage")
    status = "cap_exceeded" if over_cap else output.status
    if status not in {"completed", "build_failed", "package_failed", "invalid_output",
                      "timeout", "cap_exceeded", "infrastructure_fault"}:
        raise ValueError("worker returned an unknown status")
    validation_error = ""
    if status == "completed":
        try:
            if not isinstance(output.artifact, dict):
                raise ValueError("completed worker result has no artifact")
            if len(json.dumps(output.artifact, separators=(",", ":")).encode()) > max_output_bytes:
                raise ValueError("artifact exceeds output size limit")
            artifact = Artifact.model_validate(output.artifact)
            issues = validate_agent_artifact(artifact)
            if artifact.paper.paper_id != job["paper_id"] or issues:
                raise ValueError("artifact paper ID or agent_v1 schema is invalid")
        except (TypeError, ValueError) as exc:
            status = "invalid_output"
            validation_error = str(exc)
    log_key = (
        f"logs/{network}/{job['round_id']}/{job['hotkey']}/"
        f"{hashlib.sha256(job['paper_id'].encode()).hexdigest()}/{job['job_id']}.txt"
    )
    # The worker can print its scoped key; remove it before logs become visible.
    safe_log = output.log_bytes.replace(key.value.encode(), b"[REDACTED_PAPER_KEY]")
    if validation_error:
        safe_log += f"\n[trusted-validate] {validation_error}\n".encode()
    logs.put(log_key, safe_log)
    result = {
        "status": status,
        "artifact": output.artifact if status == "completed" else None,
        "prompt_and_completion_tokens": used,
        "token_limit": token_limit,
        "cap_source": cap_source,
        "detail": validation_error or output.detail,
    }
    backend.complete_job(
        job["job_id"], worker_id=worker_id, lease_token=job["lease_token"],
        result=result, log_object_key=log_key,
    )
    return result
