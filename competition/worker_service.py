"""Run one leased competition job with the frozen worker rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evaluation_policy import FrozenEvaluationPolicy
from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .publication import canonical_sha256
from .worker_controller import run_next_claimed_job


@dataclass(frozen=True)
class CompetitionWorkerCoordinator:
    """Bind trusted backend, Hippius, gateway and disposable VM adapters."""

    round_id: str
    network: str
    worker_id: str
    backend: Any = field(repr=False)
    paper_inputs: CompetitionPaperInputs = field(repr=False)
    packages: CompetitionPackageResolver = field(repr=False)
    evaluation: FrozenEvaluationPolicy = field(repr=False)
    pdf_fetcher: Any = field(repr=False)
    keys: Any = field(repr=False)
    execution: Any = field(repr=False)
    logs: Any = field(repr=False)

    def __post_init__(self) -> None:
        if (not self.round_id or self.network not in {"mainnet", "testnet"}
            or not self.worker_id or self.paper_inputs.round_id != self.round_id
            or self.evaluation.round_id != self.round_id
            or self.packages.backend is not self.backend
            or self.paper_inputs.backend is not self.backend):
            raise ValueError("worker coordinator has mismatched round services")

    def run_once(self) -> dict[str, Any] | None:
        record = self.backend.get_round(self.round_id)
        if (record.get("round_id") != self.round_id
            or record.get("network") != self.network
            or record.get("configuration_sha256") != self.evaluation.configuration_sha256
            or canonical_sha256(self.evaluation.configuration) != self.evaluation.configuration_sha256):
            raise ValueError("worker coordinator differs from the frozen round")
        policy = self.evaluation.configuration.get("worker_policy")
        if not isinstance(policy, dict):
            raise ValueError("frozen worker policy is missing")
        return run_next_claimed_job(
            self.round_id, backend=self.backend, worker_id=self.worker_id,
            network=self.network, paper_sources=self.paper_inputs,
            pdf_fetcher=self.pdf_fetcher, fetch_package=self.packages,
            manifest_for_job=self.packages.manifest_for_job,
            keys=self.keys, execution=self.execution, logs=self.logs,
            inference_base_url=policy["inference_base_url"],
            allowed_models=tuple(policy["allowed_models"]),
            published_token_ceiling=policy["published_token_ceiling"],
            dollar_limit=float(policy["dollar_limit_usd"]),
            lease_seconds=policy["lease_seconds"],
            max_inference_output_tokens=policy["max_inference_output_tokens"],
        )
