"""Write a one-time, post-lock duplicate review for the weekly draw."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .duplicate_candidate_setup import load_candidate_report
from .duplicate_review import duplicate_admission_record, make_duplicate_review, verify_duplicate_review
from .round_controller import _review_links
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
    report = load_candidate_report(directory / f"{round_id}.duplicate-candidates.json")
    record = duplicate_admission_record(backend, backend.get_round(round_id), report)
    submissions = [{**row, "team_id": row.get("declared_team_id", row["team_id"])}
                   for row in backend.list_submissions(round_id)]
    path = directory / f"{round_id}.duplicates.json"
    if path.exists() and record.get("status") != "locked":
        review = load_candidate_report(path)
        verify_duplicate_review(review, round_id=round_id, round_record=record,
                                submissions=submissions, candidate_report=report)
        if review["links"] != _review_links(pairs):
            raise ValueError("duplicate review already differs from provided links")
    else:
        review = make_duplicate_review(
            round_id, round_record=record,
            submissions=submissions, linked_pairs=pairs, candidate_report=report,
        )
    _write_once(path, _canonical_bytes(review), 0o600)
    return {"round_id": round_id, "links_sha256": review["links_sha256"],
            "accepted_submissions_sha256": review["accepted_submissions_sha256"]}
