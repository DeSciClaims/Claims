"""Bind post-deadline duplicate links to the locked, qualified intake."""

from __future__ import annotations

from itertools import combinations
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


def _candidate_report_hash(
    report: Mapping[str, Any], *, round_id: str,
    round_record: Mapping[str, Any], accepted_rows: list[dict[str, str]],
    accepted_sha256: str,
) -> str:
    if (report.get("schema_version") != "claims_duplicate_candidates_v1"
        or report.get("round_id") != round_id
        or report.get("archive_block_hash") != round_record.get("archive_block_hash")
        or report.get("baseline_package_sha256") != round_record.get("baseline_package_sha256")
        or report.get("configuration_sha256") != round_record.get("configuration_sha256")
        or report.get("accepted_submissions_sha256") != accepted_sha256
        or report.get("incumbent_package_sha256") != (
            round_record.get("baseline_package_sha256")
            if round_record.get("incumbent_hotkey") else None
        )):
        raise ValueError("duplicate candidate report differs from locked intake")
    packages = {row["hotkey"]: row["package_sha256"] for row in accepted_rows}
    incumbent = round_record.get("incumbent_hotkey")
    if incumbent is not None:
        if incumbent in packages:
            raise ValueError("duplicate candidate incumbent shares an entrant hotkey")
        packages[incumbent] = round_record["baseline_package_sha256"]
    errors = report.get("scan_errors")
    comparisons = report.get("comparisons")
    bases = report.get("public_base_package_sha256")
    if (not isinstance(errors, dict) or not isinstance(comparisons, list)
        or not isinstance(bases, list)
        or any(not isinstance(value, str) or len(value) != 64 for value in bases)
        or bases != sorted(set(bases))
        or any(key not in packages or not isinstance(reason, str) or not reason
               for key, reason in errors.items())):
        raise ValueError("duplicate candidate report is malformed")
    expected = set(combinations(sorted(set(packages) - set(errors)), 2))
    actual = set()
    metrics = {
        "structural_similarity", "normalized_diff_fraction",
        "shared_structural_nodes", "total_structural_nodes",
        "identical_nonbase_source_files", "shared_resource_hashes",
        "left_public_base_files_removed", "right_public_base_files_removed",
    }
    for item in comparisons:
        if not isinstance(item, dict) or not metrics.issubset(item):
            raise ValueError("duplicate candidate comparison is malformed")
        left, right = item.get("left"), item.get("right")
        if (not isinstance(left, str) or not isinstance(right, str)
            or left >= right or (left, right) not in expected
            or (left, right) in actual
            or item.get("left_package_sha256") != packages[left]
            or item.get("right_package_sha256") != packages[right]):
            raise ValueError("duplicate candidate comparison differs from locked intake")
        similarity = item["structural_similarity"]
        difference = item["normalized_diff_fraction"]
        counts = (item[name] for name in metrics - {
            "structural_similarity", "normalized_diff_fraction",
        })
        if (similarity is None and difference is not None
            or similarity is not None and (
                not isinstance(similarity, (int, float)) or isinstance(similarity, bool)
                or not 0 <= similarity <= 1
                or not isinstance(difference, (int, float))
                or isinstance(difference, bool)
                or abs(difference - (1 - similarity)) > 0.000002
            )
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for value in counts)):
            raise ValueError("duplicate candidate comparison metrics are malformed")
        actual.add((left, right))
    if actual != expected:
        raise ValueError("duplicate candidate report omits eligible comparisons")
    return canonical_sha256(report)


def make_duplicate_review(
    round_id: str, *, round_record: Mapping[str, Any],
    submissions: list[Mapping[str, Any]], linked_pairs: list[tuple[str, str, str]],
    candidate_report: Mapping[str, Any],
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
    candidate_hash = _candidate_report_hash(
        candidate_report, round_id=round_id, round_record=round_record,
        accepted_rows=rows, accepted_sha256=digest,
    )
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
        "candidate_report_sha256": candidate_hash,
        "links": links,
        "links_sha256": canonical_sha256(links),
    }


def verify_duplicate_review(
    review: Mapping[str, Any], *, round_id: str,
    round_record: Mapping[str, Any], submissions: list[Mapping[str, Any]],
    candidate_report: Mapping[str, Any],
) -> list[tuple[str, str, str]]:
    rows, digest = accepted_snapshot(submissions)
    candidate_hash = _candidate_report_hash(
        candidate_report, round_id=round_id, round_record=round_record,
        accepted_rows=rows, accepted_sha256=digest,
    )
    links = review.get("links")
    if (review.get("schema_version") != REVIEW_SCHEMA
        or review.get("round_id") != round_id
        or round_record.get("round_id") != round_id
        or round_record.get("status") not in {"locked", "revealed"}
        or review.get("archive_block_hash") != round_record.get("archive_block_hash")
        or review.get("baseline_package_sha256") != round_record.get("baseline_package_sha256")
        or review.get("incumbent_hotkey") != round_record.get("incumbent_hotkey")
        or review.get("accepted_submissions_sha256") != digest
        or review.get("candidate_report_sha256") != candidate_hash
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
