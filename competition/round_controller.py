"""Trusted lock and sample-release steps for a weekly competition round.

The backend stores each transition. The caller supplies archive, storage, and
chain adapters; none of their credentials enter a miner worker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from .drand import BlockClock, beacon_for_round
from .intake_lock import ArchiveReader, CompetitionBackend, snapshot_round
from .sample_release import PaperRecord, sample_receipt


def lock_ready_round(
    round_id: str, *, backend: CompetitionBackend, archive: ArchiveReader,
    netuid: int, package_for_upload: Callable[[Mapping[str, Any]], bytes],
) -> dict[str, Any]:
    """Freeze archive commitments and voting qualification at block D."""
    round_record = backend.get_round(round_id)
    if round_record["status"] != "open":
        raise ValueError("round must be open before lock")
    submissions = backend.list_submissions(round_id)
    hotkeys = sorted({str(item["hotkey"]) for item in submissions if item.get("status") == "uploaded"})
    qualification = backend.qualification_snapshot(round_id, hotkeys)
    if qualification["round_id"] != round_id:
        raise RuntimeError("qualification snapshot belongs to another round")
    rows = qualification["hotkeys"]
    if not isinstance(rows, list) or {item["hotkey"] for item in rows} != set(hotkeys) or len(rows) != len(hotkeys):
        raise RuntimeError("qualification snapshot is incomplete")
    qualified = {item["hotkey"] for item in rows if item["qualified"]}
    snapshot = snapshot_round(
        round_record=round_record, submissions=submissions, archive=archive,
        netuid=netuid, package_for_upload=package_for_upload,
        qualified_hotkey=lambda hotkey: hotkey in qualified,
    )
    return backend.lock_round(round_id, snapshot)


def reveal_ready_sample(
    round_id: str, *, backend: CompetitionBackend, chain: BlockClock,
    papers: list[PaperRecord], prescreen_count: int, screen_count: int,
    final_count: int, week_one: bool = False,
) -> dict[str, Any]:
    """Publish a reproducible sample after the D+600 block exists."""
    round_record = backend.get_round(round_id)
    if round_record["status"] != "locked":
        raise ValueError("round must be locked before sample release")
    beacon = beacon_for_round(round_record, chain=chain)
    receipt = sample_receipt(
        papers=papers, beacon=beacon,
        prescreen_count=prescreen_count, screen_count=screen_count,
        final_count=final_count, week_one=week_one,
    )
    return backend.reveal_sample(round_id, receipt)
