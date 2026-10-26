"""Compose local replay writes with read-only frozen inputs from the backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .pilot_worker_factory import FrozenWorkerRules
from .publication import canonical_sha256
from .replay_inputs import CompetitionReplayInputs, validate_replay_snapshot
from .replay_store import ReplayStore
from .sample_release import PaperRecord, frozen_pool_hash
from .worker_service import CompetitionWorkerCoordinator


@dataclass(frozen=True)
class ReplayRuntimeBackend:
    """Expose existing worker/scorer protocols without a primary write token."""

    store: ReplayStore
    inputs: CompetitionReplayInputs
    snapshot: dict[str, Any]
    paper_pool: tuple[PaperRecord, ...]

    def __post_init__(self) -> None:
        round_id = self.snapshot.get("round_id")
        if not isinstance(round_id, str):
            raise ValueError("replay runtime round is invalid")
        validate_replay_snapshot(self.snapshot, round_id)
        record = self.store.get_round(round_id)
        if (record["configuration_sha256"] != self.snapshot["configuration_sha256"]
            or record["network"] != self.snapshot["network"]
            or frozen_pool_hash(self.paper_pool) != self.snapshot["paper_pool_sha256"]):
            raise ValueError("replay runtime differs from stored frozen inputs")

    def __getattr__(self, name: str) -> Any:
        # Local mutation methods only; CompetitionReplayInputs has GET methods.
        return getattr(self.store, name)

    def list_paper_pool(self, round_id: str, *, reference_release_id: str) -> list[dict[str, str]]:
        if (round_id != self.snapshot["round_id"]
            or reference_release_id != self.snapshot["reference_release_id"]):
            raise ValueError("replay paper pool differs from frozen reference release")
        return [{"paper_id": row.paper_id, "source_sha256": row.source_sha256}
                for row in self.paper_pool]

    def get_bronze_source(
        self, round_id: str, *, stage: str, paper_id: str,
        reference_release_id: str,
    ) -> dict[str, Any]:
        if (round_id != self.snapshot["round_id"]
            or reference_release_id != self.snapshot["reference_release_id"]):
            raise ValueError("replay Bronze request differs from frozen reference release")
        sample = [row for row in self.store.list_sample(round_id)
                  if row["stage"] == stage and row["paper_id"] == paper_id]
        if len(sample) != 1:
            raise ValueError("replay Bronze paper is outside the local sample")
        result = self.inputs.bronze(round_id, stage, paper_id)
        if (result.get("paper_id") != paper_id
            or result.get("source_sha256") != sample[0]["source_sha256"]
            or not isinstance(result.get("artifact"), dict)
            or not isinstance(result.get("source_url"), str)
            or not result["source_url"]):
            raise ValueError("replay Bronze source differs from frozen paper")
        return result


def create_replay_worker_coordinator(
    *, backend: ReplayRuntimeBackend, worker_id: str,
    storage: Any, baseline_upload: Mapping[str, Any],
    reference_upload: Mapping[str, Any] | None,
    pdf_fetcher: Any, keys: Any, execution: Any, logs: Any,
) -> CompetitionWorkerCoordinator:
    """Use the normal worker contract with local replay jobs and GET-only inputs."""
    snapshot = backend.snapshot
    round_id = snapshot["round_id"]
    configuration = snapshot["rules_public"]["configuration"]
    rules = FrozenWorkerRules(
        round_id, snapshot["configuration_sha256"], configuration,
    )
    if canonical_sha256(configuration) != snapshot["configuration_sha256"]:
        raise ValueError("replay worker configuration differs from frozen rules")
    papers = CompetitionPaperInputs(
        backend, round_id, snapshot["reference_release_id"],
        expected_pool_sha256=snapshot["paper_pool_sha256"],
    )
    packages = CompetitionPackageResolver(
        backend, storage, baseline_upload, reference_upload,
    )
    return CompetitionWorkerCoordinator(
        round_id=round_id, network=snapshot["network"], worker_id=worker_id,
        backend=backend, paper_inputs=papers, packages=packages,
        evaluation=rules, pdf_fetcher=pdf_fetcher, keys=keys,
        execution=execution, logs=logs,
    )
