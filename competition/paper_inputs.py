"""Trusted catalog and Bronze inputs for one weekly competition round."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from .intake_lock import CompetitionBackend
from .sample_release import PaperRecord, frozen_pool_hash


@dataclass
class CompetitionPaperInputs:
    backend: CompetitionBackend
    round_id: str
    reference_release_id: str
    expected_pool_sha256: str | None = None
    _sample: dict[tuple[str, str], str] = field(default_factory=dict, init=False)
    _bronze: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not self.round_id or not self.reference_release_id:
            raise ValueError("round and reference release IDs are required")
        if (self.expected_pool_sha256 is not None
            and not re.fullmatch(r"[0-9a-f]{64}", self.expected_pool_sha256)):
            raise ValueError("expected paper pool hash is invalid")

    def papers_for_round(self, round_id: str) -> list[PaperRecord]:
        if round_id != self.round_id:
            raise ValueError("paper pool belongs to another round")
        rows = self.backend.list_paper_pool(
            round_id, reference_release_id=self.reference_release_id,
        )
        papers = [PaperRecord(str(row["paper_id"]), str(row["source_sha256"])) for row in rows]
        digest = frozen_pool_hash(papers)
        if self.expected_pool_sha256 is not None and digest != self.expected_pool_sha256:
            raise ValueError("approved paper pool changed after it was frozen")
        return papers

    def _load_sample(self) -> None:
        if self._sample:
            return
        rows = self.backend.list_sample(self.round_id)
        if not rows:
            raise ValueError("competition sample has not been released")
        for row in rows:
            key = (str(row["stage"]), str(row["paper_id"]))
            digest = str(row["source_sha256"])
            if key in self._sample or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("released sample contains malformed paper versions")
            self._sample[key] = digest

    def _source(self, stage: str, paper_id: str) -> dict[str, Any]:
        self._load_sample()
        key = (stage, paper_id)
        if key not in self._sample:
            raise ValueError("paper is not in the released stage sample")
        if key not in self._bronze:
            record = self.backend.get_bronze_source(
                self.round_id, stage=stage, paper_id=paper_id,
                reference_release_id=self.reference_release_id,
            )
            if (record.get("paper_id") != paper_id
                or record.get("source_sha256") != self._sample[key]
                or not isinstance(record.get("artifact"), dict)
                or not isinstance(record.get("source_context_by_span_id"), dict)
                or not record.get("bronze_record_id") or not record.get("full_source_id")):
                raise ValueError("Bronze record differs from the released paper")
            self._bronze[key] = record
        return self._bronze[key]

    def bronze_for_paper(self, stage: str, paper_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._source(stage, paper_id)["artifact"])

    def source_context_for_paper(self, paper_id: str) -> dict[str, str]:
        self._load_sample()
        matches = [stage for stage, selected_id in self._sample if selected_id == paper_id]
        if len(matches) != 1:
            raise ValueError("paper must appear in exactly one stage")
        context = self._source(matches[0], paper_id)["source_context_by_span_id"]
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in context.items()):
            raise ValueError("Bronze source context is malformed")
        return dict(context)
