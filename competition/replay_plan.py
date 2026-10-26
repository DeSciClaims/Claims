"""Derive a second validator's first-stage work from sealed, verified inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .commitments import verify_upload
from .duplicates import duplicate_verdicts, select_distinct_submissions
from .drand import VerifiedBeacon
from .jobs import build_jobs
from .replay_inputs import validate_replay_snapshot, verify_replay_sample
from .round_controller import _review_links
from .sample_release import PaperRecord
from .stages import stage_plan


class ReplayArchive(Protocol):
    def get_block_hash(self, block: int) -> str: ...
    def get_commitment_metadata(
        self, netuid: int, hotkey_ss58: str, block: int | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ReplayInitialPlan:
    round_id: str
    stage: str
    selected_hotkeys: tuple[str, ...]
    duplicate_verdicts: tuple[dict[str, Any], ...]
    entries: tuple[dict[str, Any], ...]
    jobs: tuple[dict[str, Any], ...]


def build_replay_initial_plan(
    snapshot: dict[str, Any], *, archive: ReplayArchive, netuid: int,
    paper_pool: list[PaperRecord], verified_beacon: VerifiedBeacon,
    package_for_upload: Callable[[Mapping[str, Any]], bytes],
    hotkey_public_key: Callable[[str], bytes],
    baseline_upload: Mapping[str, Any],
) -> ReplayInitialPlan:
    """Verify chain-bound packages and reproduce the seeded duplicate selection.

    ``baseline_upload`` is transferred separately and checked against the
    public package hash; the backend does not currently store its object key.
    This plan schedules only the initial stage. Later advancement must use the
    second validator's own paper scores rather than primary stage receipts.
    """
    round_id = snapshot.get("round_id")
    if not isinstance(round_id, str) or not round_id:
        raise ValueError("replay round ID is missing")
    validate_replay_snapshot(snapshot, round_id)
    verify_replay_sample(
        snapshot, paper_pool=paper_pool, verified_beacon=verified_beacon,
    )
    deadline = snapshot.get("deadline_block")
    archive_hash = snapshot.get("archive_block_hash")
    if (type(deadline) is not int or deadline < 0 or netuid < 0
        or not isinstance(archive_hash, str)
        or archive.get_block_hash(deadline) != archive_hash):
        raise ValueError("replay archive block differs from frozen round")
    try:
        seed = bytes.fromhex(snapshot["drand_seed"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("replay has no post-deadline draw seed") from exc
    if len(seed) != 32:
        raise ValueError("replay has no post-deadline draw seed")
    baseline_hash = snapshot.get("baseline_package_sha256")
    incumbent_hotkey = snapshot.get("incumbent_hotkey")
    if (baseline_upload.get("package_sha256") != baseline_hash
        or not isinstance(baseline_upload.get("hotkey"), str)
        or not isinstance(baseline_upload.get("team_id"), str)
        or not baseline_upload["team_id"]
        or (incumbent_hotkey is not None
            and baseline_upload["hotkey"] != incumbent_hotkey)):
        raise ValueError("replay baseline differs from frozen round")
    baseline_bytes = package_for_upload(baseline_upload)
    if (not isinstance(baseline_bytes, bytes)
        or hashlib.sha256(baseline_bytes).hexdigest() != baseline_hash
        or len(baseline_bytes) != baseline_upload.get("package_size_bytes")):
        raise ValueError("replay baseline package differs from frozen hash")
    packages: dict[str, bytes] = {}
    uploads = snapshot["uploads"]
    if not uploads:
        raise ValueError("replay has no qualified packages")
    for upload in uploads:
        hotkey = upload["hotkey"]
        package = package_for_upload(upload)
        if not isinstance(package, bytes):
            raise ValueError("replay sealed package read did not return bytes")
        record = archive.get_commitment_metadata(netuid, hotkey, block=deadline)
        verified = verify_upload(
            upload, record, package, deadline_block=deadline,
            hotkey_public_key=hotkey_public_key(hotkey),
        )
        if any(verified[key] != upload[key] for key in (
            "hotkey", "package_sha256", "salt_hex", "commitment", "commit_block",
        )):
            raise ValueError("replay package receipt differs from the archive")
        if hotkey in packages:
            raise ValueError("replay contains several packages for one hotkey")
        packages[hotkey] = package
    links = snapshot["duplicate_review_links"]
    linked_pairs = [(row["left"], row["right"], row["reason"]) for row in links]
    if _review_links(linked_pairs) != links:
        raise ValueError("replay duplicate links are not canonical")
    baseline = dict(baseline_upload)
    selection = select_distinct_submissions(
        uploads, seed=seed,
        incumbent=baseline if incumbent_hotkey else None,
        linked_pairs=linked_pairs,
        package_for_submission=lambda row: packages[row["hotkey"]],
        incumbent_package=baseline_bytes if incumbent_hotkey else None,
    )
    verdicts = duplicate_verdicts(
        selection, seed=seed, incumbent_hotkey=incumbent_hotkey,
    )
    excluded = {row["hotkey"]: row["duplicate_of_hotkey"] for row in verdicts}
    if (set(excluded) != {row["hotkey"] for row in uploads if row["status"] == "duplicate"}
        or any(row["duplicate_of_hotkey"] != excluded.get(row["hotkey"])
               for row in uploads if row["status"] == "duplicate")
        or any(row["status"] == "duplicate" for row in uploads
               if row["hotkey"] in selection.selected_hotkeys)):
        raise ValueError("replay duplicate verdict differs from seeded draw")
    selected = [row for row in uploads if row["hotkey"] in selection.selected_hotkeys]
    stages = stage_plan(
        challenger_count=len(selected), week_one=incumbent_hotkey is None,
    )
    if not stages:
        raise ValueError("replay has no distinct challenger")
    sampled_stages = {row["stage"] for row in snapshot["sample"] if row["stage"] != "rerun"}
    if sampled_stages != set(stages):
        raise ValueError("replay sample stages differ from selected challenger count")
    stage = stages[0]
    entries = [{
        "hotkey": baseline["hotkey"], "package_sha256": baseline_hash,
        "team_id": baseline["team_id"],
        "role": "incumbent" if incumbent_hotkey else "reference", "position": 0,
        "paired_gain": None,
    }]
    for position, row in enumerate(
        sorted(selected, key=lambda item: (item["commit_block"], item["hotkey"])), 1,
    ):
        entries.append({
            "hotkey": row["hotkey"], "package_sha256": row["package_sha256"],
            "team_id": row["team_id"], "role": "challenger",
            "position": position, "paired_gain": None,
        })
    jobs = build_jobs(
        round_id=round_id, selected=entries, sample=snapshot["sample"],
        stage=stage, configuration_sha256=snapshot["configuration_sha256"],
    )
    return ReplayInitialPlan(
        round_id, stage, selection.selected_hotkeys, tuple(verdicts),
        tuple(entries), tuple(jobs),
    )
