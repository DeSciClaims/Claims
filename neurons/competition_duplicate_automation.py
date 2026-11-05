"""Controller preflight for existing exact-copy rules and bounded review queues.

Similarity is retained for follow-up without delaying v2 rounds. Exact hash/AST
grouping and its seeded draw remain in competition.duplicates; no miner code is
imported or executed here. Frozen v1 policies retain their original behavior.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from competition.duplicate_candidates import MAX_ARCHIVE_BYTES, candidate_review_report
from competition.duplicate_candidate_setup import load_candidate_report
from competition.duplicate_review import (
    ADMITTED_STATUSES, accepted_snapshot, _candidate_report_hash, duplicate_admission_record,
    make_duplicate_review, verify_duplicate_review,
)
from competition.duplicates import python_source_fingerprint
from competition.hippius import HippiusPrivateStorage, hippius_s3_client
from competition.pilot_worker_factory import _required, _round_file
from competition.publication import canonical_sha256
from competition.round_setup import _canonical_bytes, _write_once


def read_policy(path: Path) -> dict:
    if path.stat().st_size > 100_000:
        raise ValueError("duplicate automation policy exceeds size limit")
    policy = json.loads(path.read_text())
    if (not isinstance(policy, dict) or set(policy) != {
        "schema_version", "enabled", "review_similarity_threshold", "public_base_archives",
    } or policy["schema_version"] not in {"claims_duplicate_automation_v1", "claims_duplicate_automation_v2"}
        or type(policy["enabled"]) is not bool
        or type(policy["review_similarity_threshold"]) not in {float, int}
        or not 0 < policy["review_similarity_threshold"] <= 1
        or not isinstance(policy["public_base_archives"], list)
        or not 1 <= len(policy["public_base_archives"]) <= 16):
        raise ValueError("duplicate automation policy is invalid")
    for item in policy["public_base_archives"]:
        if (not isinstance(item, dict) or set(item) != {"filename", "sha256"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", str(item["filename"]))
            or ".." in item["filename"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"]))):
            raise ValueError("public base identity is invalid")
    return policy


def _base_packages(path: Path, policy: dict) -> list[bytes]:
    result = []
    for item in policy["public_base_archives"]:
        archive = path.parent / item["filename"]
        if not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
            raise ValueError("public base archive exceeds limits")
        body = archive.read_bytes()
        if hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise ValueError("public base archive differs from configured checksum")
        result.append(body)
    return result


def prepare_review(row: Mapping[str, Any], backend: Any, *, policy_path: Path,
                   directory: Path, document: dict, storage: Any) -> dict:
    """Freeze automatic decisions; v2 similarity evidence never gates a round."""
    policy = read_policy(policy_path)
    if not policy["enabled"]:
        return {"status": "evaluation_ready"}
    rid = row["round_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", rid) or row["status"] != "locked":
        raise ValueError("duplicate preflight requires a locked round")
    baseline = document.get("baseline_upload")
    if (document.get("round_id") != rid
        or document.get("configuration_sha256") != row["configuration_sha256"]
        or not isinstance(baseline, dict)
        or baseline.get("package_sha256") != row["baseline_package_sha256"]):
        raise ValueError("duplicate preflight baseline differs from bound round")
    # Team resolution must not change the signed declarations in the evidence.
    submissions = [{**item, "team_id": item.get("declared_team_id", item["team_id"])}
                   for item in backend.list_submissions(rid)]
    accepted = [item for item in submissions if item["qualified"] and item["status"] in ADMITTED_STATUSES]
    incumbent = baseline if row.get("incumbent_hotkey") else None
    identities = {item["hotkey"]: item for item in accepted}
    if incumbent is not None:
        if incumbent["hotkey"] != row["incumbent_hotkey"] or incumbent["hotkey"] in identities:
            raise ValueError("duplicate preflight incumbent differs from locked intake")
        identities[incumbent["hotkey"]] = incumbent
    policy_hash = canonical_sha256(policy)
    state = dict(status="scanning", policy_sha256=policy_hash, report_sha256=None,
                 package_count=len(identities), comparison_count=0, review_pair_count=0,
                 scan_error_count=0, decision="pending", error_code=None)
    report_path = directory / f"{rid}.duplicate-candidates.json"
    review_path = directory / f"{rid}.duplicates.json"

    def publish():
        backend.runtime_request(rid, "/duplicate-review-status", dict(state))

    with (directory / f".{rid}.duplicate-preflight.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        frozen_policy = directory / f"{rid}.duplicate-policy.json"
        if frozen_policy.exists():
            policy = read_policy(frozen_policy)
        else:
            _write_once(frozen_policy, _canonical_bytes(policy), 0o600)
        policy_hash = canonical_sha256(policy)
        state["policy_sha256"] = policy_hash
        fingerprints: dict[str, str | None] = {}

        def package(hotkey):
            body = storage.package_for_submission(identities[hotkey])
            if (not isinstance(body, bytes) or not 0 < len(body) <= MAX_ARCHIVE_BYTES
                or hashlib.sha256(body).hexdigest() != identities[hotkey]["package_sha256"]):
                raise ValueError("duplicate preflight package differs from sealed receipt")
            try:
                fingerprints[hotkey] = python_source_fingerprint(body)
            except (ValueError, RecursionError):
                fingerprints[hotkey] = None
            return body

        class Packages(Mapping):
            # The scanner retains features, not every miner's complete archive.
            def __iter__(self):
                return iter(item["hotkey"] for item in accepted)
            def __len__(self):
                return len(accepted)
            def __getitem__(self, hotkey):
                return package(hotkey)

        try:
            if report_path.exists():
                report = load_candidate_report(report_path)
            else:
                publish()
                report = {**candidate_review_report(accepted, packages=Packages(),
                    incumbent=incumbent,
                    incumbent_package=package(incumbent["hotkey"]) if incumbent else None,
                    public_base_packages=_base_packages(policy_path, policy)),
                    "round_id": rid, "archive_block_hash": row["archive_block_hash"],
                    "baseline_package_sha256": row["baseline_package_sha256"],
                    "configuration_sha256": row["configuration_sha256"]}
                _write_once(report_path, _canonical_bytes(report), 0o600)
            rows, digest = accepted_snapshot(submissions)
            admission = duplicate_admission_record(backend, row, report)
            report_hash = _candidate_report_hash(report, round_id=rid, round_record=admission,
                accepted_rows=rows, accepted_sha256=digest)
            state.update(report_sha256=report_hash, comparison_count=len(report["comparisons"]),
                         scan_error_count=len(report["scan_errors"]))
            if review_path.exists():
                review = load_candidate_report(review_path)
                verify_duplicate_review(review, round_id=rid, round_record=admission,
                    submissions=submissions, candidate_report=report)
                # The receipt itself records whether this coordinator wrote it.
                origin = review.get("automation", {})
                state.update(status="complete", decision="automatic" if origin.get("policy_sha256") else "operator",
                             review_pair_count=origin.get("follow_up_review_pair_count", 0))
                publish()
                return {"status": "evaluation_ready"}
            if report["public_base_package_sha256"] != sorted({item["sha256"] for item in policy["public_base_archives"]}):
                raise ValueError("candidate report used a different public base policy")
            flagged = []
            for pair in report["comparisons"]:
                similarity = pair["structural_similarity"]
                if similarity is None or similarity < policy["review_similarity_threshold"]:
                    continue
                a, b = pair["left"], pair["right"]
                exact = identities[a]["package_sha256"] == identities[b]["package_sha256"]
                if not exact:
                    for hotkey in (a, b):
                        if hotkey not in fingerprints:
                            package(hotkey)
                    exact = bool(fingerprints[a]) and fingerprints[a] == fingerprints[b]
                if not exact:
                    flagged.append(pair)
            state["review_pair_count"] = len(flagged)
            if policy["schema_version"] == "claims_duplicate_automation_v1" and (flagged or report["scan_errors"]):
                state["status"] = "review_required"
                publish()
                return {"status": "waiting_for_duplicate_review"}
            review = make_duplicate_review(rid, round_record=admission, submissions=submissions,
                linked_pairs=[], candidate_report=report)
            review["automation"] = {"schema_version": policy["schema_version"],
                "policy_sha256": policy_hash,
                "uncertain_matches": "evaluate" if policy["schema_version"] == "claims_duplicate_automation_v2" else "review",
                "follow_up_review_pair_count": len(flagged),
                "coordinator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
            _write_once(review_path, _canonical_bytes(review), 0o600)
            state.update(status="complete", decision="automatic")
            publish()
            return {"status": "evaluation_ready"}
        except Exception:
            # Never include exception text, paths, model responses or source.
            state.update(status="error", decision="pending", error_code="scan_failed")
            publish()
            raise


def prepare_bound_round(row, backend, factory, policy_path: Path | None):
    """Resolve the baseline first, then run code preflight before judges exist."""
    result = factory.prepare_evaluation(row, backend)
    if result["status"] != "evaluation_ready" or policy_path is None:
        return result
    row = backend.get_round(row["round_id"])
    client = hippius_s3_client(endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
        access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
        secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"))
    try:
        storage = HippiusPrivateStorage(client, _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
                                       _required("CLAIMS_HIPPIUS_LOG_BUCKET"))
        return prepare_review(row, backend, policy_path=policy_path,
            directory=Path(_required("CLAIMS_COMPETITION_ROUNDS_DIR")),
            document=_round_file(row["round_id"]), storage=storage)
    finally:
        client.close()
