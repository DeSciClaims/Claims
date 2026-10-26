"""Run one leased production PDF through the active package in a disposable VM.

This path checks the package output's shape and resource use. It does not run
Bronze comparison, Silver adjudication, Gold voting, or a competition score.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from miner.agent_v1.artifact_models import Artifact
from miner.agent_v1.artifact_validator import validate_agent_artifact

from .production_reader import ActiveProductionPackage
from .worker_contract import build_worker_contract
from .worker_controller import DisposableExecution, ExecutionOutput, InferenceKeys, PrivateLogs


class ProductionResults(Protocol):
    def complete_production_job(
        self, job_id: str, *, worker_id: str, lease_token: str, result: dict[str, Any],
        log_object_key: str,
    ) -> Any: ...


def run_production_paper(
    active: ActiveProductionPackage, lease: Mapping[str, Any], *,
    network: str, worker_id: str, fetch_pdf: Callable[[str, str], bytes],
    keys: InferenceKeys, execution: DisposableExecution,
    logs: PrivateLogs, results: ProductionResults,
    inference_base_url: str, allowed_models: tuple[str, ...],
    token_limit: int, dollar_limit: float,
    is_current_release: Callable[[], bool] | None = None,
    max_inference_output_tokens: int = 8192,
    key_lifetime_seconds: int = 3600,
    max_output_bytes: int = 10_000_000,
) -> dict[str, Any]:
    """Execute one claimed production job and complete it after key revocation.

    The trusted caller owns the release reader, PDF source, lease and Bifrost
    credentials. Only the scoped paper key and verified input bundle reach the
    guest. A failed durable completion leaves the lease to expire for retry.
    """
    if network not in {"testnet", "mainnet"} or not worker_id:
        raise ValueError("production network is invalid")
    job_id = lease.get("job_id")
    paper_id = lease.get("paper_id")
    lease_token = lease.get("lease_token")
    pdf_sha256 = lease.get("source_sha256")
    source_url = lease.get("source_url")
    paper = lease.get("paper")
    attempt = lease.get("attempts")
    if (not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{64}", job_id)
        or not isinstance(paper_id, str) or not paper_id
        or not isinstance(lease_token, str) or not lease_token
        or not isinstance(pdf_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", pdf_sha256)
        or not isinstance(source_url, str) or not source_url
        or not isinstance(paper, dict) or paper.get("paper_id") != paper_id
        or isinstance(attempt, bool) or attempt not in {1, 2}
        or lease.get("receipt_sha256") != active.receipt_sha256
        or lease.get("package_sha256") != active.package_sha256):
        raise ValueError("production lease differs from the active release or PDF")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", active.round_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", active.hotkey)
        or active.manifest is None or not isinstance(active.manifest, dict)
        or hashlib.sha256(active.package).hexdigest() != active.package_sha256
        or not 1 <= key_lifetime_seconds <= 7200 or token_limit < 1
        or dollar_limit <= 0 or max_output_bytes < 1):
        raise ValueError("production release or resource budget is invalid")
    job = {
        "job_id": job_id, "round_id": active.round_id, "hotkey": active.hotkey,
        "package_sha256": active.package_sha256, "paper_id": paper_id,
        "lease_token": lease_token, "paper": paper,
        "manifest": active.manifest,
    }
    contract = build_worker_contract(
        job, allowed_models=allowed_models,
        max_output_tokens=max_inference_output_tokens,
    )
    pdf = fetch_pdf(source_url, pdf_sha256)
    if hashlib.sha256(pdf).hexdigest() != pdf_sha256:
        raise ValueError("production PDF differs from its pinned source hash")
    if is_current_release is not None and not is_current_release():
        raise ValueError("production release changed before paper execution")
    key = keys.create_paper_key(
        round_id=active.round_id, paper_id=paper_id, hotkey=active.hotkey,
        allowed_models=contract["allowed_models"], token_limit=token_limit,
        dollar_limit=dollar_limit,
        expires_at=datetime.now(UTC) + timedelta(seconds=key_lifetime_seconds),
    )
    try:
        try:
            output = execution.run(
                job={**job, "worker_contract": contract,
                     "expected_pdf_sha256": pdf_sha256},
                package=active.package, pdf=pdf,
                inference_base_url=inference_base_url, paper_key=key.value,
            )
        except Exception as exc:
            output = ExecutionOutput(
                "infrastructure_fault", None,
                f"worker transport failed: {type(exc).__name__}".encode(),
            )
        usage = keys.check_budget(key)
    finally:
        keys.revoke(key)
    if is_current_release is not None and not is_current_release():
        raise ValueError("production release changed during paper execution")
    if (isinstance(usage.tokens, bool) or usage.tokens < 0
        or not math.isfinite(usage.dollar_cost_usd) or usage.dollar_cost_usd < 0
        or isinstance(output.denied_egress_packets, bool)
        or not isinstance(output.denied_egress_packets, int)
        or output.denied_egress_packets < 0
        or not isinstance(output.log_bytes, bytes)
        or not isinstance(output.detail, str)):
        raise ValueError("production worker returned invalid resource usage")
    vm_fields = (output.vm_elapsed_seconds, output.vm_cost_estimate_usd)
    if any(value is not None for value in vm_fields) and any(
        value is None or isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value < 0 for value in vm_fields
    ):
        raise ValueError("production worker returned invalid VM cost estimate")
    status = ("security_violation" if output.denied_egress_packets
              else "cap_exceeded" if usage.over_cap else output.status)
    if status not in {"completed", "build_failed", "package_failed", "invalid_output",
                      "timeout", "cap_exceeded", "infrastructure_fault", "security_violation"}:
        raise ValueError("production worker returned an unknown status")
    detail = output.detail.replace(key.value, "[REDACTED_PAPER_KEY]")
    artifact = output.artifact if status == "completed" else None
    if status == "completed":
        try:
            if not isinstance(artifact, dict):
                raise ValueError("completed production run has no artifact")
            if len(json.dumps(artifact, separators=(",", ":"), allow_nan=False).encode()) > max_output_bytes:
                raise ValueError("production artifact exceeds the output limit")
            checked = Artifact.model_validate(artifact)
            issues = validate_agent_artifact(checked)
            if checked.paper.paper_id != paper_id or issues:
                raise ValueError("production artifact paper ID or agent_v1 schema is invalid")
        except (TypeError, ValueError) as exc:
            status, artifact, detail = "invalid_output", None, str(exc)
    log_key = (
        f"logs/{network}/{active.round_id}/{active.hotkey}/"
        f"{hashlib.sha256(paper_id.encode()).hexdigest()}/"
        f"{job_id}-attempt-{attempt}.txt"
    )
    safe_log = output.log_bytes.replace(key.value.encode(), b"[REDACTED_PAPER_KEY]")
    if status == "invalid_output":
        safe_log += f"\n[trusted-validate] {detail}\n".encode()
    logs.put(log_key, safe_log)
    result = {
        "status": status, "artifact": artifact,
        "round_id": active.round_id, "hotkey": active.hotkey,
        "receipt_sha256": active.receipt_sha256,
        "package_sha256": active.package_sha256,
        "paper_id": paper_id, "source_sha256": pdf_sha256,
        "prompt_and_completion_tokens": usage.tokens,
        "token_limit": token_limit,
        "inference_cost_usd": usage.dollar_cost_usd,
        "denied_egress_packets": output.denied_egress_packets,
        "vm_elapsed_seconds": output.vm_elapsed_seconds,
        "vm_cost_estimate_usd": output.vm_cost_estimate_usd,
        "detail": detail,
    }
    results.complete_production_job(
        job_id, worker_id=worker_id, lease_token=lease_token, result=result,
        log_object_key=log_key,
    )
    return result
