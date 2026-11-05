"""Produce a private, post-lock code-comparison report for review."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

from .code_audit_setup import _read_json
from .duplicate_candidates import MAX_ARCHIVE_BYTES, candidate_review_report
from .duplicate_review import (
    ADMITTED_STATUSES, _candidate_report_hash, accepted_snapshot, duplicate_admission_record,
)
from .publication import canonical_sha256
from .round_setup import _canonical_bytes, _write_once


def load_candidate_report(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        body = handle.read(20_000_001)
    if len(body) > 20_000_000:
        raise ValueError("duplicate candidate report exceeds twenty megabytes")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("duplicate candidate report must be a JSON object")
    return result


def prepare_candidate_report(
    round_id: str, *, backend: Any, storage: Any, directory: Path,
    public_base_packages: list[bytes] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id) or not directory.is_dir():
        raise ValueError("candidate report round or directory is invalid")
    record = backend.get_round(round_id)
    report_path = directory / f"{round_id}.duplicate-candidates.json"
    existing = load_candidate_report(report_path) if report_path.exists() else None
    if int((record or {}).get("rules_revision", 1)) > 1:
        from .rule_amendments import read_private_round, sync_current_rules
        record = sync_current_rules(backend, record, directory=directory)
        document = read_private_round(directory, round_id)
    else:
        document = _read_json(directory / f"{round_id}.json")
    if (record is None or record.get("round_id") != round_id
        or (record.get("status") != "locked"
            and not (existing is not None and int(record.get("rules_revision", 1)) > 1
                     and record.get("status") in {"revealed", "screening", "final", "gold", "gold_closed"}))
        or document.get("round_id") != round_id
        or document.get("configuration_sha256") != record.get("configuration_sha256")
        or not isinstance(document.get("baseline_upload"), dict)
        or document["baseline_upload"].get("package_sha256")
            != record.get("baseline_package_sha256")):
        raise ValueError("candidate report differs from locked round")
    submissions = [{**row, "team_id": row.get("declared_team_id", row["team_id"])}
                   for row in backend.list_submissions(round_id)]
    selected = [row for row in submissions
                if row.get("status") in ADMITTED_STATUSES and row.get("qualified")]
    packages = {row["hotkey"]: storage.package_for_submission(row) for row in selected}
    baseline = document["baseline_upload"]
    incumbent = baseline if record.get("incumbent_hotkey") else None
    if incumbent is not None and incumbent.get("hotkey") != record["incumbent_hotkey"]:
        raise ValueError("candidate report incumbent differs from frozen round")
    incumbent_package = storage.package_for_submission(baseline) if incumbent else None
    bases = public_base_packages or []
    if any(not isinstance(package, bytes) or not 0 < len(package) <= MAX_ARCHIVE_BYTES
           for package in bases):
        raise ValueError("public base archive exceeds candidate scanner limit")
    if existing is not None:
        admission = duplicate_admission_record(backend, record, existing)
        rows, accepted_hash = accepted_snapshot(submissions)
        digest = _candidate_report_hash(existing, round_id=round_id, round_record=admission,
                                        accepted_rows=rows, accepted_sha256=accepted_hash)
        if any(hashlib.sha256(packages[row["hotkey"]]).hexdigest() != row["package_sha256"]
               for row in selected) or (incumbent is not None
               and hashlib.sha256(incumbent_package).hexdigest() != incumbent["package_sha256"]):
            raise ValueError("duplicate candidate package differs from sealed receipt")
        if bases and existing["public_base_package_sha256"] != sorted({hashlib.sha256(p).hexdigest() for p in bases}):
            raise ValueError("candidate report used different public base archives")
        return {"round_id": round_id, "report_sha256": digest,
                "comparison_count": len(existing["comparisons"]),
                "scan_error_count": len(existing["scan_errors"])}
    comparison = candidate_review_report(
        selected, packages=packages, incumbent=incumbent,
        incumbent_package=incumbent_package, public_base_packages=bases,
    )
    report = {
        **comparison,
        "round_id": round_id,
        "archive_block_hash": record["archive_block_hash"],
        "baseline_package_sha256": record["baseline_package_sha256"],
        "configuration_sha256": record["configuration_sha256"],
    }
    digest = canonical_sha256(report)
    _write_once(report_path, _canonical_bytes(report), 0o600)
    return {
        "round_id": round_id, "report_sha256": digest,
        "comparison_count": len(report["comparisons"]),
        "scan_error_count": len(report["scan_errors"]),
    }
