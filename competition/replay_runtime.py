"""Compose local replay writes with read-only frozen inputs from the backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .replay_inputs import CompetitionReplayInputs, validate_replay_snapshot
from .replay_store import ReplayStore
from .sample_release import PaperRecord, frozen_pool_hash


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
