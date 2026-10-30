"""Trusted one-paper job lifecycle around a disposable miner worker VM."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from .bifrost import PaperKey, PaperUsage
from .worker_contract import build_worker_contract
from .worker_errors import WorkerTransportError
from miner.agent_v1.artifact_models import Artifact
from miner.agent_v1.artifact_validator import validate_agent_artifact


@dataclass(frozen=True)
class ExecutionOutput:
    status: str
    artifact: dict[str, Any] | None
    log_bytes: bytes
    detail: str = ""
    denied_egress_packets: int = 0
    vm_elapsed_seconds: float | None = None
    vm_cost_estimate_usd: float | None = None


class InferenceKeys(Protocol):
    def create_paper_key(
        self, *, round_id: str, paper_id: str, hotkey: str,
        allowed_models: tuple[str, ...], token_limit: int,
        dollar_limit: float, expires_at: datetime,
    ) -> PaperKey: ...
    def check_budget(self, key: PaperKey) -> PaperUsage: ...
    def revoke(self, key: PaperKey) -> None: ...


class DisposableExecution(Protocol):
    def run(
        self, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput: ...


class PrivateLogs(Protocol):
    def put(self, key: str, content: bytes) -> None: ...


class JobBackend(Protocol):
    def claim_job(
        self, round_id: str, *, worker_id: str, lease_seconds: int,
    ) -> dict[str, Any] | None: ...
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


def remaining_attempt_budget(
    job: dict[str, Any], *, paper_token_limit: int, dollar_limit: float,
) -> tuple[int, float, str | None]:
    """A fresh retry key may use only the first attempt's unspent allowance."""
    if job.get("attempts", 1) == 1:
        return paper_token_limit, dollar_limit, None
    prior = job.get("result_private")
    if not isinstance(prior, dict) or prior.get("billing_observed") is not True:
        return 0, 0.0, "unmetered_prior_attempt"
    tokens = prior.get("prompt_and_completion_tokens")
    cost = prior.get("inference_cost_usd")
    if (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
        or isinstance(cost, bool) or not isinstance(cost, int | float)
        or not math.isfinite(cost) or cost < 0):
        return 0, 0.0, "unmetered_prior_attempt"
    remaining_tokens = paper_token_limit - tokens
    remaining_dollars = dollar_limit - cost
    if remaining_tokens < 1 or remaining_dollars <= 0:
        return 0, 0.0, "prior_attempt_exhausted_budget"
    return remaining_tokens, remaining_dollars, None


def run_claimed_job(
    job: dict[str, Any], *, backend: JobBackend,
    worker_id: str, network: str,
    fetch_package: Callable[[dict[str, Any]], bytes],
    fetch_pdf: Callable[[dict[str, Any]], bytes],
    expected_pdf_sha256: str,
    keys: InferenceKeys, execution: DisposableExecution,
    logs: PrivateLogs, inference_base_url: str,
    allowed_models: tuple[str, ...], published_token_ceiling: int,
    dollar_limit: float, key_lifetime_seconds: int = 3600,
    max_output_bytes: int = 10_000_000,
    max_inference_output_tokens: int = 8192,
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
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", str(job["hotkey"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(job["job_id"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(job["package_sha256"]))
        or not re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha256)):
        raise ValueError("claimed job hash or storage path component is invalid")
    if network not in {"testnet", "mainnet"} or not worker_id:
        raise ValueError("worker identity or network is invalid")
    attempt = job.get("attempts", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt not in {1, 2}:
        raise ValueError("worker lease attempt must be one or two")
    if (not 1 <= key_lifetime_seconds <= 3600 or published_token_ceiling < 1
        or dollar_limit <= 0 or max_output_bytes < 1):
        raise ValueError("paper key budget or lifetime is invalid")
    package = fetch_package(job)
    if hashlib.sha256(package).hexdigest() != job["package_sha256"]:
        raise ValueError("sealed package differs from claimed job hash")
    pdf = fetch_pdf(job)
    if hashlib.sha256(pdf).hexdigest() != expected_pdf_sha256:
        raise ValueError("paper PDF differs from released sample hash")
    contract = build_worker_contract(
        job, allowed_models=allowed_models,
        max_output_tokens=max_inference_output_tokens,
    )
    paper_limit, cap_source = paper_token_cap(
        job, backend=backend, published_ceiling=published_token_ceiling,
    )
    token_limit, attempt_dollars, stop_reason = remaining_attempt_budget(
        job, paper_token_limit=paper_limit, dollar_limit=dollar_limit,
    )
    log_key = (
        f"logs/{network}/{job['round_id']}/{job['hotkey']}/"
        f"{hashlib.sha256(job['paper_id'].encode()).hexdigest()}/"
        f"{job['job_id']}-attempt-{attempt}.txt"
    )
    if stop_reason is not None:
        status = ("cap_exceeded" if stop_reason == "prior_attempt_exhausted_budget"
                  else "infrastructure_fault")
        result = {
            "status": status, "artifact": None,
            "prompt_and_completion_tokens": 0, "inference_cost_usd": 0.0,
            "vm_elapsed_seconds": None, "vm_cost_estimate_usd": None,
            "denied_egress_packets": 0, "token_limit": 0,
            "paper_token_limit": paper_limit, "cap_source": cap_source,
            "detail": stop_reason,
        }
        logs.put(log_key, f"[trusted-budget] {stop_reason}\n".encode())
        backend.complete_job(
            job["job_id"], worker_id=worker_id, lease_token=job["lease_token"],
            result=result, log_object_key=log_key,
        )
        return result
    key = keys.create_paper_key(
        round_id=job["round_id"], paper_id=job["paper_id"], hotkey=job["hotkey"],
        allowed_models=contract["allowed_models"], token_limit=token_limit,
        dollar_limit=attempt_dollars,
        expires_at=datetime.now(UTC) + timedelta(seconds=key_lifetime_seconds),
    )
    try:
        try:
            output = execution.run(
                job={**job, "worker_contract": contract,
                     "expected_pdf_sha256": expected_pdf_sha256},
                package=package, pdf=pdf,
                inference_base_url=inference_base_url, paper_key=key.value,
            )
        except Exception as exc:
            reason = exc.reason if isinstance(exc, WorkerTransportError) else type(exc).__name__
            output = ExecutionOutput("infrastructure_fault", None,
                                     f"worker transport failed: {reason}".encode(),
                                     detail=reason)
        usage = keys.check_budget(key)
    finally:
        keys.revoke(key)
    if usage.tokens < 0 or not math.isfinite(usage.dollar_cost_usd) or usage.dollar_cost_usd < 0:
        raise ValueError("gateway returned invalid usage")
    if (isinstance(output.denied_egress_packets, bool)
        or not isinstance(output.denied_egress_packets, int)
        or output.denied_egress_packets < 0
        or (output.status == "security_violation") != bool(output.denied_egress_packets)):
        raise ValueError("worker returned an invalid denied-egress counter")
    vm_fields = (output.vm_elapsed_seconds, output.vm_cost_estimate_usd)
    if any(value is not None for value in vm_fields) and (
        any(value is None or isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 for value in vm_fields)
    ):
        raise ValueError("worker returned invalid VM cost estimate")
    status = ("security_violation" if output.denied_egress_packets
              else "cap_exceeded" if usage.over_cap else output.status)
    if status not in {"completed", "build_failed", "package_failed", "invalid_output",
                      "timeout", "cap_exceeded", "infrastructure_fault", "security_violation"}:
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
    # The worker can print its scoped key; remove it before logs become visible.
    safe_log = output.log_bytes.replace(key.value.encode(), b"[REDACTED_PAPER_KEY]")
    if validation_error:
        safe_log += f"\n[trusted-validate] {validation_error}\n".encode()
    logs.put(log_key, safe_log)
    result = {
        "status": status,
        "artifact": output.artifact if status == "completed" else None,
        "prompt_and_completion_tokens": usage.tokens,
        "inference_cost_usd": usage.dollar_cost_usd,
        "vm_elapsed_seconds": output.vm_elapsed_seconds,
        "vm_cost_estimate_usd": output.vm_cost_estimate_usd,
        "denied_egress_packets": output.denied_egress_packets,
        "token_limit": token_limit,
        "paper_token_limit": paper_limit,
        "cap_source": cap_source,
        "detail": validation_error or output.detail,
    }
    backend.complete_job(
        job["job_id"], worker_id=worker_id, lease_token=job["lease_token"],
        result=result, log_object_key=log_key,
    )
    return result


class PaperSources(Protocol):
    def pdf_source_for_paper(self, stage: str, paper_id: str) -> tuple[str, str]: ...
    def paper_metadata_for_paper(self, stage: str, paper_id: str) -> dict[str, Any]: ...


class PdfFetcher(Protocol):
    def fetch(self, url: str, expected_sha256: str) -> bytes: ...


def run_next_claimed_job(
    round_id: str, *, backend: JobBackend, worker_id: str,
    network: str, paper_sources: PaperSources, pdf_fetcher: PdfFetcher,
    fetch_package: Callable[[dict[str, Any]], bytes],
    manifest_for_job: Callable[[dict[str, Any]], dict[str, Any]],
    keys: InferenceKeys, execution: DisposableExecution, logs: PrivateLogs,
    inference_base_url: str, allowed_models: tuple[str, ...],
    published_token_ceiling: int, dollar_limit: float,
    lease_seconds: int = 3600, max_inference_output_tokens: int = 8192,
) -> dict[str, Any] | None:
    """Lease one job and stage only its frozen PDF bytes on the worker."""
    job = backend.claim_job(round_id, worker_id=worker_id, lease_seconds=lease_seconds)
    if job is None:
        return None
    if job.get("round_id") != round_id or job.get("status") != "leased":
        raise ValueError("backend returned a job outside the requested lease")
    source_url, source_sha256 = paper_sources.pdf_source_for_paper(
        str(job["stage"]), str(job["paper_id"]),
    )
    enriched_job = {
        **job,
        "manifest": manifest_for_job(job),
        "paper": paper_sources.paper_metadata_for_paper(
            str(job["stage"]), str(job["paper_id"]),
        ),
    }
    return run_claimed_job(
        enriched_job, backend=backend, worker_id=worker_id, network=network,
        fetch_package=fetch_package,
        fetch_pdf=lambda _: pdf_fetcher.fetch(source_url, source_sha256),
        expected_pdf_sha256=source_sha256,
        keys=keys, execution=execution, logs=logs,
        inference_base_url=inference_base_url,
        allowed_models=allowed_models,
        published_token_ceiling=published_token_ceiling,
        dollar_limit=dollar_limit,
        key_lifetime_seconds=min(lease_seconds, 3600),
        max_inference_output_tokens=max_inference_output_tokens,
    )
