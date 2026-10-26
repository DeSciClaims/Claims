"""Detect when a failed incumbent requires a fresh reference comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class PaperJobs(Protocol):
    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class IncumbentFailureSummary:
    stage: str
    eligible_papers: int
    failed_papers: int

    @property
    def reference_required(self) -> bool:
        return 5 * self.failed_papers > self.eligible_papers


class ReferenceFallbackRequired(RuntimeError):
    def __init__(self, summary: IncumbentFailureSummary):
        self.summary = summary
        super().__init__(
            f"{summary.stage} incumbent failed on {summary.failed_papers}/"
            f"{summary.eligible_papers} eligible papers; reference comparison required"
        )


def incumbent_failure_summary(
    round_id: str, *, stage: str, paper_ids: list[str],
    excluded_infrastructure_papers: set[str], incumbent_hotkey: str,
    backend: PaperJobs,
) -> IncumbentFailureSummary:
    """Exclude global infrastructure faults, then apply the strict >20% rule."""
    if (not paper_ids or len(set(paper_ids)) != len(paper_ids)
        or not excluded_infrastructure_papers <= set(paper_ids)
        or not incumbent_hotkey):
        raise ValueError("incumbent failure audit has an invalid paper grid")
    failed = 0
    eligible = 0
    for paper_id in paper_ids:
        if paper_id in excluded_infrastructure_papers:
            continue
        jobs = backend.list_paper_jobs(round_id, stage, paper_id)
        incumbent = [row for row in jobs if row.get("hotkey") == incumbent_hotkey]
        if len(incumbent) != 1 or incumbent[0].get("status") != "completed":
            raise ValueError("incumbent failure audit lacks a completed baseline job")
        result = incumbent[0].get("result_private")
        if not isinstance(result, dict) or result.get("status") not in {
            "completed", "build_failed", "package_failed", "invalid_output",
            "timeout", "cap_exceeded", "security_violation", "infrastructure_fault",
        }:
            raise ValueError("incumbent failure audit has an unknown worker status")
        if result["status"] == "infrastructure_fault":
            raise ValueError("incumbent infrastructure fault was not excluded globally")
        eligible += 1
        failed += int(result["status"] != "completed")
    if not eligible:
        raise ValueError("incumbent failure audit has no eligible papers")
    return IncumbentFailureSummary(stage, eligible, failed)
