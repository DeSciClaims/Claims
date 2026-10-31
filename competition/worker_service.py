"""Run one leased competition job with the frozen worker rules."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import Lock
import copy
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Any

from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .policy_runtime import policy_runtime_config
from .publication import canonical_sha256
from .worker_controller import run_next_claimed_job


class _BatchPackages:
    """Keep one sealed package in trusted RAM for its concurrent paper batch."""

    def __init__(self, source):
        self.source, self.backend = source, source.backend
        self.lock = Lock()
        self.identity = None
        self.manifest = self.package = None

    def _check(self, job):
        identity = (job["round_id"], job["stage"], job["hotkey"], job["package_sha256"])
        if self.identity is not None and identity != self.identity:
            raise ValueError("execution batch cache received another package")
        self.identity = identity

    def manifest_for_job(self, job):
        with self.lock:
            self._check(job)
            if self.manifest is None:
                self.manifest = self.source.manifest_for_job(job)
            return copy.deepcopy(self.manifest)

    def __call__(self, job):
        with self.lock:
            self._check(job)
            if self.package is None:
                self.package = self.source(job)
            return self.package


@dataclass(frozen=True)
class CompetitionWorkerCoordinator:
    """Bind trusted backend, Hippius, gateway and disposable VM adapters."""

    round_id: str
    network: str
    worker_id: str
    backend: Any = field(repr=False)
    paper_inputs: CompetitionPaperInputs = field(repr=False)
    packages: CompetitionPackageResolver = field(repr=False)
    evaluation: Any = field(repr=False)
    pdf_fetcher: Any = field(repr=False)
    keys: Any = field(repr=False)
    execution: Any = field(repr=False)
    logs: Any = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.round_id
            or self.network not in {"mainnet", "testnet"}
            or not self.worker_id
            or self.paper_inputs.round_id != self.round_id
            or self.evaluation.round_id != self.round_id
            or self.packages.backend is not self.backend
            or self.paper_inputs.backend is not self.backend
        ):
            raise ValueError("worker coordinator has mismatched round services")

    def run_once(self, *, claimed_job=None) -> dict[str, Any] | None:
        record = self.backend.get_round(self.round_id)
        if (
            record.get("round_id") != self.round_id
            or record.get("network") != self.network
            or record.get("configuration_sha256")
            != self.evaluation.configuration_sha256
            or self.evaluation.configuration.get("competition_policy_runtime")
            != policy_runtime_config()
            or canonical_sha256(self.evaluation.configuration)
            != self.evaluation.configuration_sha256
        ):
            raise ValueError("worker coordinator differs from the frozen round")
        policy = self.evaluation.configuration.get("worker_policy")
        if not isinstance(policy, dict):
            raise ValueError("frozen worker policy is missing")
        return run_next_claimed_job(
            self.round_id,
            backend=self.backend,
            worker_id=self.worker_id,
            network=self.network,
            paper_sources=self.paper_inputs,
            pdf_fetcher=self.pdf_fetcher,
            fetch_package=self.packages,
            manifest_for_job=self.packages.manifest_for_job,
            keys=self.keys,
            execution=self.execution,
            logs=self.logs,
            inference_base_url=policy["inference_base_url"],
            allowed_models=tuple(policy["allowed_models"]),
            published_token_ceiling=policy["published_token_ceiling"],
            dollar_limit=float(policy["dollar_limit_usd"]),
            lease_seconds=policy["lease_seconds"],
            max_inference_output_tokens=policy["max_inference_output_tokens"],
            **({"claimed_job": claimed_job} if claimed_job is not None else {}),
        )

    def run_available(self) -> list[dict[str, Any]]:
        """Fill memory slots with one package's papers and refill on completion.

        Jobs are leased only when a slot is available. Each completion persists
        immediately through the existing run_once path, independent of siblings.
        A process crash leaves only the usual bounded job leases to reconcile.
        """
        capacity = getattr(self.execution, "capacity", None)
        if not callable(capacity):
            result = self.run_once()
            return [] if result is None else [result]
        expected_memory = self.evaluation.configuration["worker_policy"].get(
            "memory_per_job_bytes"
        )

        def admitted_capacity():
            result = capacity()
            if (
                expected_memory is not None
                and result.get("memory_per_job_bytes") != expected_memory
            ):
                raise ValueError("worker memory limit differs from frozen round")
            return result

        limits = admitted_capacity()
        slots = limits["slots"]
        if slots == 0 or limits["available_slots"] == 0:
            return []
        if type(slots) is not int or not 1 <= slots <= 64:
            raise ValueError("worker returned invalid execution capacity")
        lease = self.evaluation.configuration["worker_policy"]["lease_seconds"]
        package = None
        runner = replace(self, packages=_BatchPackages(self.packages))
        results, failures = [], []
        with ThreadPoolExecutor(max_workers=slots) as pool:
            running = set()
            exhausted = False
            paused = False
            while True:
                available = (
                    0
                    if paused
                    else min(
                        slots - len(running), admitted_capacity()["available_slots"]
                    )
                )
                for _ in range(available):
                    kwargs = {"worker_id": self.worker_id, "lease_seconds": lease}
                    if package is not None:
                        kwargs.update(hotkey=package[0], stage=package[2])
                    job = self.backend.claim_job(self.round_id, **kwargs)
                    if job is None:
                        exhausted = True
                        break
                    identity = (job["hotkey"], job["package_sha256"], job["stage"])
                    if package is not None and identity != package:
                        raise ValueError(
                            "backend mixed packages or stages in an execution batch"
                        )
                    package = identity
                    running.add(pool.submit(runner.run_once, claimed_job=job))
                if not running:
                    break
                completed, running = wait(running, return_when=FIRST_COMPLETED)
                for future in completed:
                    try:
                        result = future.result()
                        if result["status"] == "capacity_deferred":
                            paused = True
                        results.append({"status": result["status"]})
                    except Exception as exc:
                        paused = True
                        failures.append(exc)
                # Recheck same-package readiness after every result; no wave barrier.
                if exhausted and not running:
                    break
                exhausted = False
        if failures:
            raise RuntimeError(
                f"{len(failures)} paper executions need lease recovery"
            ) from failures[0]
        return results
