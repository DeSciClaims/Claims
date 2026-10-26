"""Trusted lock and sample-release steps for a weekly competition round.

The backend stores each transition. The caller supplies archive, storage, and
chain adapters; none of their credentials enter a miner worker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from .drand import BlockClock, beacon_for_round
from .duplicates import duplicate_verdicts, select_distinct_submissions
from .intake_lock import ArchiveReader, CompetitionBackend, snapshot_round
from .sample_release import PaperRecord, sample_receipt
from .stage_controller import start_initial_stage
from .stages import stage_plan


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


def reveal_selected_sample(
    round_id: str, *, backend: CompetitionBackend, chain: BlockClock,
    papers: list[PaperRecord], baseline: Mapping[str, Any],
    package_for_submission: Callable[[Mapping[str, Any]], bytes],
    final_count: int, incumbent_package: bytes | None = None,
    linked_pairs: list[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """Draw distinct entrants and release exactly their required stage papers.

    The same verified post-deadline beacon drives both the duplicate lottery
    and paper sample. The duplicate receipts are stored after sample release
    by ``start_revealed_round``; that step replays and checks this draw.
    """
    round_record = backend.get_round(round_id)
    if round_record["status"] != "locked":
        raise ValueError("round must be locked before sample release")
    week_one = round_record["incumbent_hotkey"] is None
    if week_one and final_count != 320:
        raise ValueError("week-one launch sample must contain 320 papers")
    if not week_one and final_count < 500:
        raise ValueError("staged final sample must contain at least 500 papers")
    if baseline["package_sha256"] != round_record["baseline_package_sha256"]:
        raise ValueError("baseline differs from frozen round")
    if round_record["incumbent_hotkey"] is not None and baseline["hotkey"] != round_record["incumbent_hotkey"]:
        raise ValueError("incumbent differs from frozen round")
    beacon = beacon_for_round(round_record, chain=chain)
    submissions = [row for row in backend.list_submissions(round_id)
                   if row["status"] == "accepted" and row["qualified"]]
    selection = select_distinct_submissions(
        submissions, seed=beacon.seed,
        incumbent=baseline if round_record["incumbent_hotkey"] else None,
        package_for_submission=package_for_submission,
        incumbent_package=incumbent_package,
        linked_pairs=linked_pairs,
    )
    stages = stage_plan(
        challenger_count=len(selection.selected_hotkeys),
        week_one=round_record["incumbent_hotkey"] is None,
    )
    if not stages:
        return {"status": "waiting_for_entrants"}
    receipt = sample_receipt(
        papers=papers, beacon=beacon,
        prescreen_count=20 if "prescreen" in stages else 0,
        screen_count=120 if "screen" in stages else 0,
        final_count=final_count,
        week_one="week_one" in stages,
    )
    result = backend.reveal_sample(round_id, receipt)
    return {**result, "selected_count": len(selection.selected_hotkeys),
            "stages": list(stages)}


def start_revealed_round(
    round_id: str, *, backend: CompetitionBackend,
    baseline: Mapping[str, Any],
    package_for_submission: Callable[[Mapping[str, Any]], bytes],
    incumbent_package: bytes | None = None,
    linked_pairs: list[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """Replay the seeded duplicate draw and start the first stage once.

    Duplicate receipts are written before jobs begin. A restart recomputes the
    draw from all frozen accepted/duplicate uploads, then checks existing
    receipts; it cannot silently change the selected package after a crash.
    """
    round_record = backend.get_round(round_id)
    stages = backend.list_stages(round_id)
    if stages:
        return {"status": "already_started", "stage": stages[0]["stage"]}
    if round_record["status"] != "revealed":
        raise ValueError("sample must be revealed before duplicate selection")
    seed = bytes.fromhex(round_record["drand_seed"])
    if len(seed) != 32:
        raise ValueError("round has no verified 32-byte draw seed")
    if baseline["package_sha256"] != round_record["baseline_package_sha256"]:
        raise ValueError("baseline differs from frozen round")
    if round_record["incumbent_hotkey"] is not None and baseline["hotkey"] != round_record["incumbent_hotkey"]:
        raise ValueError("incumbent differs from frozen round")
    submissions = [row for row in backend.list_submissions(round_id)
                   if row["status"] in {"accepted", "duplicate"} and row["qualified"]]
    if not submissions:
        return {"status": "waiting_for_entrants"}
    selection = select_distinct_submissions(
        submissions, seed=seed,
        incumbent=baseline if round_record["incumbent_hotkey"] else None,
        package_for_submission=package_for_submission,
        incumbent_package=incumbent_package,
        linked_pairs=linked_pairs,
    )
    verdicts = duplicate_verdicts(
        selection, seed=seed, incumbent_hotkey=round_record["incumbent_hotkey"],
    )
    stored_by_hotkey = {row["hotkey"]: row for row in submissions}
    for verdict in verdicts:
        stored = stored_by_hotkey[verdict["hotkey"]]
        if stored["status"] == "duplicate" and (
            stored["duplicate_of_hotkey"] != verdict["duplicate_of_hotkey"]
            or stored["duplicate_reason"] != verdict["reason"]
            or stored["duplicate_evidence"] != verdict["evidence"]
        ):
            raise ValueError("stored duplicate verdict differs from seeded draw")
    if set(row["hotkey"] for row in submissions if row["status"] == "duplicate") != {
        verdict["hotkey"] for verdict in verdicts if stored_by_hotkey[verdict["hotkey"]]["status"] == "duplicate"
    }:
        raise ValueError("stored duplicate is absent from seeded draw")
    selected = set(selection.selected_hotkeys)
    challengers = [row for row in submissions if row["hotkey"] in selected]
    if not challengers:
        return {"status": "waiting_for_entrants"}
    expected_stages = set(stage_plan(
        challenger_count=len(challengers),
        week_one=round_record["incumbent_hotkey"] is None,
    ))
    released_stages = {row["stage"] for row in backend.list_sample(round_id)}
    if released_stages != expected_stages:
        raise ValueError("released sample stages differ from seeded entrant count")
    pending = [verdict for verdict in verdicts
               if stored_by_hotkey[verdict["hotkey"]]["status"] == "accepted"]
    if pending:
        backend.mark_duplicates(round_id, pending)
    return start_initial_stage(round_id, backend=backend,
                               challengers=challengers, baseline=baseline)
