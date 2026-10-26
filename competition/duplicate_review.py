"""Bind post-deadline duplicate links to the locked, qualified intake."""

from __future__ import annotations

from typing import Any, Mapping

from .publication import canonical_sha256
from .round_controller import _review_links


REVIEW_SCHEMA = "claims_duplicate_review_v1"


def accepted_snapshot(submissions: list[Mapping[str, Any]]) -> tuple[list[dict[str, str]], str]:
    rows = sorted(({
        "hotkey": row["hotkey"], "team_id": row["team_id"],
        "package_sha256": row["package_sha256"],
    } for row in submissions if row["status"] in {"accepted", "duplicate"}
        and row["qualified"]), key=lambda row: row["hotkey"])
    if len({row["hotkey"] for row in rows}) != len(rows):
        raise ValueError("locked duplicate review has repeated hotkeys")
    return rows, canonical_sha256(rows)


def make_duplicate_review(
    round_id: str, *, round_record: Mapping[str, Any],
    submissions: list[Mapping[str, Any]], linked_pairs: list[tuple[str, str, str]],
) -> dict[str, Any]:
    if round_record.get("round_id") != round_id or round_record.get("status") != "locked":
        raise ValueError("duplicate review requires a locked round")
    archive_hash = round_record.get("archive_block_hash")
    baseline_hash = round_record.get("baseline_package_sha256")
    incumbent_hotkey = round_record.get("incumbent_hotkey")
    if (not isinstance(archive_hash, str) or not archive_hash
        or not isinstance(baseline_hash, str) or len(baseline_hash) != 64
        or (incumbent_hotkey is not None
            and (not isinstance(incumbent_hotkey, str) or not incumbent_hotkey))):
        raise ValueError("duplicate review lacks the frozen baseline or archive block")
    rows, digest = accepted_snapshot(submissions)
    hotkeys = {row["hotkey"] for row in rows}
    if incumbent_hotkey is not None:
        hotkeys.add(incumbent_hotkey)
    links = _review_links(linked_pairs)
    if any(link["left"] not in hotkeys or link["right"] not in hotkeys for link in links):
        raise ValueError("duplicate review links an ineligible submission")
    return {
        "schema_version": REVIEW_SCHEMA, "round_id": round_id,
        "archive_block_hash": archive_hash,
        "baseline_package_sha256": baseline_hash,
        "incumbent_hotkey": incumbent_hotkey,
        "accepted_submissions_sha256": digest,
        "links": links,
        "links_sha256": canonical_sha256(links),
    }


def verify_duplicate_review(
    review: Mapping[str, Any], *, round_id: str,
    round_record: Mapping[str, Any], submissions: list[Mapping[str, Any]],
) -> list[tuple[str, str, str]]:
    rows, digest = accepted_snapshot(submissions)
    links = review.get("links")
    if (review.get("schema_version") != REVIEW_SCHEMA
        or review.get("round_id") != round_id
        or round_record.get("round_id") != round_id
        or round_record.get("status") not in {"locked", "revealed"}
        or review.get("archive_block_hash") != round_record.get("archive_block_hash")
        or review.get("baseline_package_sha256") != round_record.get("baseline_package_sha256")
        or review.get("incumbent_hotkey") != round_record.get("incumbent_hotkey")
        or review.get("accepted_submissions_sha256") != digest
        or not isinstance(links, list)
        or review.get("links_sha256") != canonical_sha256(links)):
        raise ValueError("duplicate review differs from locked intake")
    pairs = []
    for link in links:
        if not isinstance(link, dict) or set(link) != {"left", "right", "reason"}:
            raise ValueError("duplicate review link is malformed")
        pairs.append((link["left"], link["right"], link["reason"]))
    if links != _review_links(pairs):
        raise ValueError("duplicate review links are not canonical")
    hotkeys = {row["hotkey"] for row in rows}
    if round_record.get("incumbent_hotkey") is not None:
        hotkeys.add(round_record["incumbent_hotkey"])
    if any(left not in hotkeys or right not in hotkeys for left, right, _ in pairs):
        raise ValueError("duplicate review links an ineligible submission")
    if round_record["status"] == "revealed" and (
        round_record.get("duplicate_review_links") != links
        or round_record.get("duplicate_review_sha256") != review["links_sha256"]
    ):
        raise ValueError("duplicate review differs from revealed sample")
    return pairs
