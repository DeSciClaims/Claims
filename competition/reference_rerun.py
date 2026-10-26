"""Freeze a replacement reference comparison on the failed stage's papers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .failure_policy import IncumbentFailureSummary


@dataclass(frozen=True)
class ReferenceRerunPlan:
    source_stage: str
    papers: tuple[dict[str, Any], ...]
    entries: tuple[dict[str, Any], ...]


def reference_rerun_plan(
    *, failure: IncumbentFailureSummary, round_record: Mapping[str, Any],
    source_receipt: Mapping[str, Any], sample: list[Mapping[str, Any]],
    reference: Mapping[str, Any],
) -> ReferenceRerunPlan:
    """Use only the precommitted reference and the original frozen entrant grid.

    The backend must recheck the failure threshold and persist this plan
    atomically. This function has no authority to change a released sample.
    """
    stage = failure.stage
    if stage not in {"prescreen", "screen", "final"} or not failure.reference_required:
        raise ValueError("reference rerun requires an eligible failed incumbent stage")
    if source_receipt.get("stage") != stage or not round_record.get("incumbent_hotkey"):
        raise ValueError("rerun source differs from the incumbent round")
    originals = [dict(row) for row in sample if row.get("stage") == stage]
    if (not originals or len({row["paper_id"] for row in originals}) != len(originals)
        or sorted(row["position"] for row in originals) != list(range(len(originals)))):
        raise ValueError("rerun source has no complete distinct paper sample")
    if not 0 < failure.failed_papers <= failure.eligible_papers <= len(originals):
        raise ValueError("rerun failure count differs from frozen paper sample")
    entries = list(source_receipt["entries"])
    incumbent = [row for row in entries if row["role"] == "incumbent"]
    challengers = [row for row in entries if row["role"] == "challenger"]
    if (len(incumbent) != 1 or not challengers
        or incumbent[0]["hotkey"] != round_record["incumbent_hotkey"]
        or incumbent[0]["package_sha256"] != round_record["baseline_package_sha256"]
        or len(entries) != len(challengers) + 1):
        raise ValueError("rerun source does not match frozen incumbent entrants")
    reference_hash = round_record.get("reference_package_sha256")
    if (not isinstance(reference_hash, str) or len(reference_hash) != 64
        or reference.get("package_sha256") != reference_hash
        or not reference.get("hotkey") or not reference.get("team_id")
        or reference["hotkey"] in {row["hotkey"] for row in entries}):
        raise ValueError("rerun reference differs from frozen round")
    papers = tuple({**row, "stage": "rerun"} for row in sorted(originals, key=lambda row: row["position"]))
    selected = ({
        "hotkey": str(reference["hotkey"]),
        "package_sha256": reference_hash,
        "team_id": str(reference["team_id"]),
        "role": "reference", "position": 0, "paired_gain": None,
    }, *({**row, "position": position, "paired_gain": None}
         for position, row in enumerate(sorted(challengers, key=lambda row: row["position"]), 1)))
    return ReferenceRerunPlan(stage, papers, selected)
