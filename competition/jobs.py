"""Deterministic work IDs for idempotent paper/submission evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def job_id(
    *, round_id: str, hotkey: str, package_sha256: str,
    stage: str, paper_id: str, configuration_sha256: str,
) -> str:
    fields = [round_id, hotkey, package_sha256, stage, paper_id, configuration_sha256]
    if any(not field for field in fields):
        raise ValueError("job identity fields must be nonempty")
    encoded = json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(b"claims-competition-job-v1\0" + encoded).hexdigest()


def build_jobs(
    *, round_id: str, selected: list[Mapping[str, Any]],
    sample: list[Mapping[str, Any]], configuration_sha256: str,
) -> list[dict[str, Any]]:
    """Build one job per eligible package per paper after duplicate selection."""
    jobs: list[dict[str, Any]] = []
    hotkeys: set[str] = set()
    for submission in selected:
        if not submission.get("qualified"):
            continue
        hotkey = str(submission["hotkey"])
        if hotkey in hotkeys:
            raise ValueError("selected packages contain a duplicate hotkey")
        hotkeys.add(hotkey)
        package_sha256 = str(submission["package_sha256"])
        for paper in sample:
            stage, paper_id = str(paper["stage"]), str(paper["paper_id"])
            jobs.append({
                "job_id": job_id(
                    round_id=round_id, hotkey=hotkey, package_sha256=package_sha256,
                    stage=stage, paper_id=paper_id,
                    configuration_sha256=configuration_sha256,
                ),
                "hotkey": hotkey,
                "package_sha256": package_sha256,
                "stage": stage,
                "paper_id": paper_id,
                "configuration_sha256": configuration_sha256,
            })
    return jobs
