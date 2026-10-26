"""Write a one-time, post-lock duplicate review for the weekly draw."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .duplicate_candidate_setup import load_candidate_report
from .duplicate_review import make_duplicate_review
from .round_setup import _canonical_bytes, _write_once


def prepare_duplicate_review(
    round_id: str, *, backend: Any, directory: Path,
    links: list[Mapping[str, str]],
) -> dict[str, str]:
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id)
        or not directory.is_dir() or not isinstance(links, list)):
        raise ValueError("duplicate review input is malformed")
    pairs = []
    for link in links:
        if not isinstance(link, dict) or set(link) != {"left", "right", "reason"}:
            raise ValueError("duplicate review link is malformed")
        pairs.append((link["left"], link["right"], link["reason"]))
    review = make_duplicate_review(
        round_id, round_record=backend.get_round(round_id),
        submissions=backend.list_submissions(round_id), linked_pairs=pairs,
        candidate_report=load_candidate_report(
            directory / f"{round_id}.duplicate-candidates.json"),
    )
    _write_once(directory / f"{round_id}.duplicates.json", _canonical_bytes(review), 0o600)
    return {"round_id": round_id, "links_sha256": review["links_sha256"],
            "accepted_submissions_sha256": review["accepted_submissions_sha256"]}
