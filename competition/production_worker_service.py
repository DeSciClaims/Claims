"""Poll durable production jobs and run the currently active sealed package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from .production_reader import ActiveProductionPackage, ProductionReleaseReader
from .production_runner import run_production_paper


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

    def run_once(self) -> dict[str, Any] | None:
        try:
            start = datetime.fromisoformat(self.production_start_at)
        except ValueError as exc:
            raise ValueError("production start timestamp is invalid") from exc
        if (self.network not in {"testnet", "mainnet"} or not self.worker_id
            or start.tzinfo is None or not 1 <= self.producer_batch_size <= 1000):
            raise ValueError("production worker identity is invalid")
        active = self.reader.read_active()
        if active is None:
            return None
        policy = self.rules_for_release(active)
        if not isinstance(policy, Mapping):
            raise ValueError("active production release has no frozen worker policy")
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
        lease = self.backend.claim_production_job(
            network=self.network, receipt_sha256=active.receipt_sha256,
            worker_id=self.worker_id, lease_seconds=policy["lease_seconds"],
        )
        if lease is None:
            return None
        if not isinstance(lease, dict):
            raise ValueError("production backend returned a malformed lease")
        return run_production_paper(
            active, lease, network=self.network, worker_id=self.worker_id,
            fetch_pdf=self.pdf_fetcher.fetch,
            keys=self.keys, execution=self.execution, logs=self.logs,
            results=self.backend,
            inference_base_url=policy["inference_base_url"],
            allowed_models=tuple(policy["allowed_models"]),
            token_limit=policy["published_token_ceiling"],
            dollar_limit=float(policy["dollar_limit_usd"]),
            is_current_release=lambda: (
                (current := self.reader.read_active()) is not None
                and current.receipt_sha256 == active.receipt_sha256
                and current.package_sha256 == active.package_sha256
            ),
            max_inference_output_tokens=policy["max_inference_output_tokens"],
        )
