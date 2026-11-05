import json

import pytest

from competition.duplicate_review import (
    accepted_snapshot, duplicate_admission_record, make_duplicate_review, verify_duplicate_review,
)
from competition.duplicate_review_setup import prepare_duplicate_review
from competition.publication import canonical_sha256


class Backend:
    def __init__(self):
        self.record = {
            "round_id": "week1", "status": "locked", "archive_block_hash": "0x" + "a" * 64,
            "baseline_package_sha256": "9" * 64, "incumbent_hotkey": None,
            "configuration_sha256": "c" * 64,
        }
        self.submissions = [{
            "hotkey": key, "team_id": team, "package_sha256": digest * 64,
            "status": "accepted", "qualified": True,
        } for key, team, digest in (("a", "team-a", "1"), ("b", "team-b", "2"))]

    def get_round(self, _):
        return self.record

    def list_submissions(self, _):
        return self.submissions


def _candidate(backend):
    rows, digest = accepted_snapshot(backend.submissions)
    packages = {row["hotkey"]: row["package_sha256"] for row in rows}
    if backend.record["incumbent_hotkey"]:
        packages[backend.record["incumbent_hotkey"]] = backend.record["baseline_package_sha256"]
    keys = sorted(packages)
    comparisons = [{
        "left": left, "right": right,
        "left_package_sha256": packages[left], "right_package_sha256": packages[right],
        "structural_similarity": 0.5, "normalized_diff_fraction": 0.5,
        "shared_structural_nodes": 1, "total_structural_nodes": 2,
        "identical_nonbase_source_files": 0, "shared_resource_hashes": 0,
        "left_public_base_files_removed": 0, "right_public_base_files_removed": 0,
    } for index, left in enumerate(keys) for right in keys[index + 1:]]
    return {
        "schema_version": "claims_duplicate_candidates_v1",
        "round_id": "week1", "archive_block_hash": backend.record["archive_block_hash"],
        "baseline_package_sha256": backend.record["baseline_package_sha256"],
        "configuration_sha256": backend.record["configuration_sha256"],
        "accepted_submissions_sha256": digest,
        "incumbent_package_sha256": (backend.record["baseline_package_sha256"]
                                     if backend.record["incumbent_hotkey"] else None),
        "public_base_package_sha256": [], "scan_errors": {},
        "comparisons": comparisons,
    }


def test_post_lock_duplicate_review_is_write_once_and_bound_to_intake(tmp_path):
    backend = Backend()
    candidate = _candidate(backend)
    (tmp_path / "week1.duplicate-candidates.json").write_text(json.dumps(candidate))
    links = [{"left": "b", "right": "a", "reason": "reviewed structural match"}]
    first = prepare_duplicate_review(
        "week1", backend=backend, directory=tmp_path, links=links,
    )
    assert prepare_duplicate_review(
        "week1", backend=backend, directory=tmp_path, links=links,
    ) == first
    path = tmp_path / "week1.duplicates.json"
    assert path.stat().st_mode & 0o777 == 0o600
    review = json.loads(path.read_text())
    assert review["links"] == [{"left": "a", "right": "b",
                                "reason": "reviewed structural match"}]
    assert verify_duplicate_review(
        review, round_id="week1", round_record=backend.record,
        submissions=backend.submissions, candidate_report=candidate,
    ) == [("a", "b", "reviewed structural match")]
    with pytest.raises(ValueError, match="already differs"):
        prepare_duplicate_review("week1", backend=backend, directory=tmp_path, links=[])
    backend.submissions[1]["package_sha256"] = "3" * 64
    with pytest.raises(ValueError, match="locked intake"):
        verify_duplicate_review(
            review, round_id="week1", round_record=backend.record,
            submissions=backend.submissions, candidate_report=candidate,
        )


def test_revealed_round_requires_exact_review_hash_and_links():
    backend = Backend()
    candidate = _candidate(backend)
    review = make_duplicate_review(
        "week1", round_record=backend.record, submissions=backend.submissions,
        linked_pairs=[("a", "b", "same code")], candidate_report=candidate,
    )
    backend.record.update(status="revealed", duplicate_review_links=review["links"],
                          duplicate_review_sha256=canonical_sha256(review["links"]))
    assert verify_duplicate_review(
        review, round_id="week1", round_record=backend.record,
        submissions=backend.submissions, candidate_report=candidate,
    ) == [("a", "b", "same code")]
    backend.record["duplicate_review_links"] = []
    with pytest.raises(ValueError, match="revealed sample"):
        verify_duplicate_review(
            review, round_id="week1", round_record=backend.record,
            submissions=backend.submissions, candidate_report=candidate,
        )


def test_review_rejects_prelock_and_ineligible_links():
    backend = Backend()
    backend.record["status"] = "open"
    with pytest.raises(ValueError, match="locked round"):
        make_duplicate_review(
            "week1", round_record=backend.record, submissions=backend.submissions,
            linked_pairs=[], candidate_report=_candidate(backend),
        )
    backend.record["status"] = "locked"
    backend.submissions[1]["qualified"] = False
    candidate = _candidate(backend)
    with pytest.raises(ValueError, match="ineligible"):
        make_duplicate_review(
            "week1", round_record=backend.record, submissions=backend.submissions,
            linked_pairs=[("a", "b", "same code")], candidate_report=candidate,
        )


def test_review_can_bind_near_copy_to_frozen_incumbent():
    backend = Backend()
    backend.record["incumbent_hotkey"] = "king"
    candidate = _candidate(backend)
    review = make_duplicate_review(
        "week1", round_record=backend.record, submissions=backend.submissions,
        linked_pairs=[("king", "a", "reviewed near copy")], candidate_report=candidate,
    )
    assert verify_duplicate_review(
        review, round_id="week1", round_record=backend.record,
        submissions=backend.submissions, candidate_report=candidate,
    ) == [("a", "king", "reviewed near copy")]
    backend.record["baseline_package_sha256"] = "8" * 64
    with pytest.raises(ValueError, match="locked intake"):
        verify_duplicate_review(
            review, round_id="week1", round_record=backend.record,
            submissions=backend.submissions, candidate_report=candidate,
        )


def test_review_rejects_candidate_report_missing_pair():
    backend = Backend()
    candidate = _candidate(backend)
    candidate["comparisons"] = []
    with pytest.raises(ValueError, match="omits eligible comparisons"):
        make_duplicate_review(
            "week1", round_record=backend.record, submissions=backend.submissions,
            linked_pairs=[], candidate_report=candidate,
        )


def test_amendment_history_authorizes_an_immutable_report_without_waiving_intake_checks(tmp_path):
    backend = Backend()
    candidate = _candidate(backend)
    (tmp_path / "week1.duplicate-candidates.json").write_text(json.dumps(candidate))
    original = prepare_duplicate_review("week1", backend=backend, directory=tmp_path, links=[])
    receipt = (tmp_path / "week1.duplicates.json").read_bytes()
    backend.record.update(rules_revision=2, configuration_sha256="d" * 64)
    backend.list_rule_amendments = lambda *a, **k: {
        "round_id": "week1", "rules_revision": 2, "amendments": [{
            "from_revision": 1, "to_revision": 2,
            "previous_configuration_sha256": "c" * 64, "configuration_sha256": "d" * 64}]}
    backend.submissions[0]["status"] = "finished"
    backend.submissions[1]["status"] = "evaluating"
    assert prepare_duplicate_review("week1", backend=backend, directory=tmp_path, links=[]) == original
    assert (tmp_path / "week1.duplicates.json").read_bytes() == receipt
    backend.record["archive_block_hash"] = "changed"
    with pytest.raises(ValueError, match="locked intake"):
        prepare_duplicate_review("week1", backend=backend, directory=tmp_path, links=[])


def test_historical_duplicate_config_follows_bounded_continuous_pages():
    backend = Backend()
    old = _candidate(backend)
    backend.record.update(rules_revision=4, configuration_sha256="f" * 64)
    calls = []
    def history(rid, *, limit, before_revision):
        calls.append((rid, limit, before_revision))
        rows = [{"from_revision": n - 1, "to_revision": n,
                 "configuration_sha256": current * 64, "previous_configuration_sha256": previous * 64}
                for n, current, previous in [(4, "f", "e"), (3, "e", "d"), (2, "d", "c")]]
        return {"round_id": rid, "rules_revision": 4,
                "amendments": rows[:2] if before_revision is None else rows[2:]}
    backend.list_rule_amendments = history
    admission = duplicate_admission_record(backend, backend.record, old)
    assert admission["configuration_sha256"] == "c" * 64
    assert backend.record["configuration_sha256"] == "f" * 64
    assert calls == [("week1", 100, None), ("week1", 100, 3)]
    old["configuration_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="absent from the approved"):
        duplicate_admission_record(backend, backend.record, old)


@pytest.mark.parametrize("damage", ["round_id", "rules_revision", "gap", "config"])
def test_historical_duplicate_config_rejects_changed_or_malformed_history(damage):
    backend = Backend()
    old = _candidate(backend)
    backend.record.update(rules_revision=2, configuration_sha256="d" * 64)
    page = {"round_id": "week1", "rules_revision": 2, "amendments": [{
        "from_revision": 1, "to_revision": 2,
        "previous_configuration_sha256": "c" * 64, "configuration_sha256": "d" * 64}]}
    if damage in {"round_id", "rules_revision"}:
        page[damage] = "different"
    elif damage == "gap":
        page["amendments"][0]["from_revision"] = 0
    else:
        page["amendments"][0]["configuration_sha256"] = "e" * 64
    backend.list_rule_amendments = lambda *a, **k: page
    with pytest.raises(ValueError, match="history"):
        duplicate_admission_record(backend, backend.record, old)
