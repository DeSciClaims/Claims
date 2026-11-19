"""Run durable production jobs in memory-admitted, single-package batches."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import math
from time import monotonic
from typing import Any, Callable, Mapping

from .production_reader import ActiveProductionPackage, ProductionReleaseReader
from .production_runner import run_production_paper
from .production_checkpoints import checkpoint_matches, usage_already_recorded


@dataclass(frozen=True)
class ProductionWorkerCoordinator:
    network: str
    worker_id: str
    production_start_at: str
    backend: Any = field(repr=False)
    reader: ProductionReleaseReader = field(repr=False)
    rules_for_release: Callable[[ActiveProductionPackage], Mapping[str, Any]] = field(repr=False)
    pdf_fetcher: Any = field(repr=False)
    keys: Any = field(repr=False)
    execution: Any = field(repr=False)
    logs: Any = field(repr=False)
    producer_batch_size: int = 50
    max_parallel_jobs: int = 10
    competition_parallel_jobs: int = 4
    competition_check_seconds: float = 10
    checkpoints: Any = field(default=None, repr=False)

    def _validate(self) -> None:
        try:
            start = datetime.fromisoformat(self.production_start_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("production start timestamp is invalid") from exc
        if (self.network not in {"testnet", "mainnet"} or not self.worker_id
            or start.tzinfo is None
            or type(self.producer_batch_size) is not int or not 1 <= self.producer_batch_size <= 1000
            or type(self.max_parallel_jobs) is not int or not 1 <= self.max_parallel_jobs <= 64
            or type(self.competition_parallel_jobs) is not int
            or not 1 <= self.competition_parallel_jobs <= self.max_parallel_jobs
            or type(self.competition_check_seconds) not in {int, float}
            or not 1 <= self.competition_check_seconds <= 600):
            raise ValueError("production worker scheduling settings are invalid")

    @staticmethod
    def _identity(active: ActiveProductionPackage | None) -> tuple | None:
        return None if active is None else (active.receipt_sha256, active.package_sha256)

    def _enqueue(self, active: ActiveProductionPackage) -> None:
        pending = self.backend.pending_production_papers(
            network=self.network, receipt_sha256=active.receipt_sha256,
            created_after=self.production_start_at, limit=self.producer_batch_size,
        )
        if not isinstance(pending, list):
            raise ValueError("production backend returned invalid paper backlog")
        for row in pending:
            if not isinstance(row, dict):
                raise ValueError("production backlog has a malformed paper")
            paper = {"paper_id": row["paper_id"], "title": row["title"]}
            if row.get("doi"):
                paper["doi"] = row["doi"]
            self.backend.enqueue_production_job({
                "network": self.network, "round_id": active.round_id,
                "receipt_sha256": active.receipt_sha256,
                "hotkey": active.hotkey,
                "package_sha256": active.package_sha256,
                "paper_id": row["paper_id"], "paper": paper,
                "source_url": row["source_url"],
                "source_sha256": row["source_sha256"],
            })

    def _run_claimed(self, active, policy, lease) -> dict[str, Any]:
        if not isinstance(lease, dict):
            raise ValueError("production backend returned a malformed lease")
        # A lease pins the signed release. A subsequent handoff stops new
        # admissions, but must not discard already paid-for extraction results.
        try:
            return run_production_paper(
                active, lease, network=self.network, worker_id=self.worker_id,
                fetch_pdf=self.pdf_fetcher.fetch,
                keys=self.keys, execution=self.execution, logs=self.logs,
                results=self.backend,
                inference_base_url=policy["inference_base_url"],
                allowed_models=tuple(policy["allowed_models"]),
                token_limit=policy["published_token_ceiling"],
                dollar_limit=float(policy["dollar_limit_usd"]),
                key_lifetime_seconds=min(policy["lease_seconds"], 3600),
                max_inference_output_tokens=policy["max_inference_output_tokens"],
                checkpoints=self.checkpoints,
            )
        except Exception as exc:
            # A paper-level control/cleanup error is durable work to recover,
            # not a reason to cancel healthy siblings. Persistence failures
            # still escape: never pretend a lost result has been recorded.
            snapshot = self.checkpoints.find(lease["job_id"]) if self.checkpoints is not None else None
            if snapshot is not None and not checkpoint_matches(snapshot, active, lease, self.network):
                snapshot = None
            usage = (snapshot or {}).get("usage") or {}
            key_id = (snapshot or {}).get("virtual_key_id")
            cost, tokens = usage.get("dollar_cost_usd"), usage.get("tokens")
            known = (type(cost) in {int, float} and math.isfinite(cost) and cost >= 0
                     and type(tokens) is int and tokens >= 0)
            if known and usage_already_recorded(lease, key_id):
                cost, tokens = 0.0, 0
            if not known:
                cost, tokens = None, None
            log_key = (f"logs/{self.network}/{active.round_id}/{active.hotkey}/"
                       f"{hashlib.sha256(lease['paper_id'].encode()).hexdigest()}/"
                       f"{lease['job_id']}-attempt-{lease['attempts']}.txt")
            detail = "paper_processing_failed: " + type(exc).__name__
            result = {"status": "infrastructure_fault", "artifact": None,
                "round_id": active.round_id, "hotkey": active.hotkey,
                "receipt_sha256": active.receipt_sha256, "package_sha256": active.package_sha256,
                "paper_id": lease["paper_id"], "source_sha256": lease["source_sha256"],
                "inference_cost_usd": cost, "prompt_and_completion_tokens": tokens,
                "paper_token_limit": policy["published_token_ceiling"],
                "usage_virtual_key_id": key_id, "recovery_required": True, "detail": detail}
            self.logs.put(log_key, ("[trusted-paper] " + detail + "\n").encode())
            saved = self.backend.complete_production_job(lease["job_id"], worker_id=self.worker_id,
                lease_token=lease["lease_token"], result=result, log_object_key=log_key)
            if saved is not True and not (isinstance(saved, dict) and saved.get("completed") is True):
                raise RuntimeError("paper failure could not be durably recorded") from exc
            return result

    def run_once(self) -> dict[str, Any] | None:
        """Compatibility path for one durable paper lease."""
        self._validate()
        active = self.reader.read_active()
        if active is None:
            return None
        policy = self.rules_for_release(active)
        if not isinstance(policy, Mapping):
            raise ValueError("active production release has no frozen worker policy")
        self._enqueue(active)
        lease = self.backend.claim_production_job(
            network=self.network, receipt_sha256=active.receipt_sha256,
            worker_id=self.worker_id, lease_seconds=policy["lease_seconds"],
        )
        return None if lease is None else self._run_claimed(active, policy, lease)

    def _parallel_limit(self) -> int:
        # Backend outage/malformed state must not silently grant full capacity.
        rounds = self.backend.list_pending_rounds(self.network)
        if (not isinstance(rounds, list)
            or any(not isinstance(row, dict) or row.get("network") != self.network
                   or not isinstance(row.get("status"), str) for row in rounds)):
            raise ValueError("production competition-activity response is invalid")
        busy = any(row["status"] in {"revealed", "screening", "final"} for row in rounds)
        return self.competition_parallel_jobs if busy else self.max_parallel_jobs

    def _capacity(self, policy) -> dict:
        capacity = getattr(self.execution, "capacity", None)
        if not callable(capacity):
            raise ValueError("parallel production requires a capacity-aware worker")

        info = capacity()
        if (not isinstance(info, dict) or info.get("schema_version") != "claims_worker_capacity_v1"
            or type(info.get("slots")) is not int or not 0 <= info["slots"] <= 64
            or type(info.get("available_slots")) is not int
            or not 0 <= info["available_slots"] <= info["slots"]
            or policy.get("memory_per_job_bytes", info.get("memory_per_job_bytes"))
               != info.get("memory_per_job_bytes")):
            raise ValueError("production worker returned invalid memory capacity")
        return info

    def check_ready(self) -> dict:
        """Read-only startup check: no jobs, keys, model calls or PDF downloads."""
        self._validate()
        active = self.reader.read_active()
        if active is None:
            raise ValueError("production has no activated signed release")
        policy = self.rules_for_release(active)
        if not isinstance(policy, Mapping):
            raise ValueError("active production release has no frozen worker policy")
        info = self._capacity(policy)
        limit = self._parallel_limit()
        pending = self.backend.pending_production_papers(
            network=self.network, receipt_sha256=active.receipt_sha256,
            created_after=self.production_start_at, limit=1,
        )
        if not isinstance(pending, list):
            raise ValueError("production backend returned invalid paper backlog")
        return {"status": "production_ready", "network": self.network,
                "round_id": active.round_id, "package_sha256": active.package_sha256,
                "worker_slots": info["slots"], "available_slots": info["available_slots"],
                "max_parallel_jobs": self.max_parallel_jobs,
                "competition_parallel_jobs": self.competition_parallel_jobs,
                "current_parallel_limit": min(limit, info["slots"]),
                "production_start_at": self.production_start_at,
                "backlog_available": bool(pending)}

    def run_available(self, *, on_completed: Callable[[dict], None] | None = None) -> list[dict]:
        """Refill available slots and persist each paper independently.

        One cycle is bounded by producer_batch_size. It never pre-leases waiting
        work or holds full extraction artifacts in the batch result. Errors stop
        admissions while sibling jobs finish; backend leases retain retry/budget
        state. Competition activity reduces admissions, without killing jobs.
        """
        self._validate()
        active = self.reader.read_active()
        if active is None:
            return []
        policy = self.rules_for_release(active)
        if not isinstance(policy, Mapping):
            raise ValueError("active production release has no frozen worker policy")
        initial = self._capacity(policy)
        if not initial["available_slots"]:
            return []
        limit = self._parallel_limit()
        checked_at = monotonic()
        self._enqueue(active)
        results, errors = [], []
        admitted = 0
        paused = False

        def execute(lease):
            result = self._run_claimed(active, policy, lease)
            # The runner has already durably stored the artifact, usage and log
            # receipt. Only small non-sensitive completion metadata leaves it.
            return {"status": result["status"], "paper_id": lease.get("paper_id"),
                    "job_id": lease["job_id"], "round_id": active.round_id}

        with ThreadPoolExecutor(max_workers=min(initial["slots"], self.max_parallel_jobs)) as pool:
            running = set()
            while True:
                try:
                    if not paused and self._identity(self.reader.read_active()) != self._identity(active):
                        paused = True
                    if not paused and monotonic() - checked_at >= self.competition_check_seconds:
                        limit = self._parallel_limit()
                        checked_at = monotonic()
                    info = self._capacity(policy) if not paused else {"slots": 0, "available_slots": 0}
                    # Also cap the number of leased futures, not just executing
                    # threads: queued executor futures would age their leases.
                    free_threads = min(limit, initial["slots"], info["slots"]) - len(running)
                    available = max(0, min(free_threads, info["available_slots"],
                                           self.producer_batch_size - admitted))
                    for _ in range(available):
                        lease = self.backend.claim_production_job(
                            network=self.network, receipt_sha256=active.receipt_sha256,
                            worker_id=self.worker_id, lease_seconds=policy["lease_seconds"],
                        )
                        if lease is None:
                            break
                        if not isinstance(lease, dict):
                            raise ValueError("production backend returned a malformed lease")
                        running.add(pool.submit(execute, lease))
                        admitted += 1
                except Exception as exc:
                    paused = True
                    errors.append(type(exc).__name__)
                if not running:
                    break
                completed, running = wait(running, return_when=FIRST_COMPLETED)
                for future in completed:
                    try:
                        result = future.result()
                        results.append(result)
                        if on_completed is not None:
                            on_completed(dict(result))
                    except Exception as exc:
                        paused = True
                        errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("production batch paused; inspect durable job leases: " + ",".join(sorted(set(errors))))
        return results
