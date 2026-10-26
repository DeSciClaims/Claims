import json

import pytest

from competition.duplicate_review import make_duplicate_review, verify_duplicate_review
from competition.duplicate_review_setup import prepare_duplicate_review
from competition.publication import canonical_sha256


class Backend:
    def __init__(self):
        self.record = {
            "round_id": "week1", "status": "locked", "archive_block_hash": "0x" + "a" * 64,
            "baseline_package_sha256": "9" * 64, "incumbent_hotkey": None,
        }
        self.submissions = [{
            "hotkey": key, "team_id": team, "package_sha256": digest * 64,
            "status": "accepted", "qualified": True,
        } for key, team, digest in (("a", "team-a", "1"), ("b", "team-b", "2"))]

    def get_round(self, _):
        return self.record

    def list_submissions(self, _):
        return self.submissions


def test_post_lock_duplicate_review_is_write_once_and_bound_to_intake(tmp_path):
    backend = Backend()
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
        submissions=backend.submissions,
    ) == [("a", "b", "reviewed structural match")]
    with pytest.raises(ValueError, match="already differs"):
        prepare_duplicate_review("week1", backend=backend, directory=tmp_path, links=[])
    backend.submissions[1]["package_sha256"] = "3" * 64
    with pytest.raises(ValueError, match="locked intake"):
        verify_duplicate_review(
            review, round_id="week1", round_record=backend.record,
            submissions=backend.submissions,
        )


def test_revealed_round_requires_exact_review_hash_and_links():
    backend = Backend()
    review = make_duplicate_review(
        "week1", round_record=backend.record, submissions=backend.submissions,
        linked_pairs=[("a", "b", "same code")],
    )
    backend.record.update(status="revealed", duplicate_review_links=review["links"],
                          duplicate_review_sha256=canonical_sha256(review["links"]))
    assert verify_duplicate_review(
        review, round_id="week1", round_record=backend.record,
        submissions=backend.submissions,
    ) == [("a", "b", "same code")]
    backend.record["duplicate_review_links"] = []
    with pytest.raises(ValueError, match="revealed sample"):
        verify_duplicate_review(
            review, round_id="week1", round_record=backend.record,
            submissions=backend.submissions,
        )


def test_review_rejects_prelock_and_ineligible_links():
    backend = Backend()
    backend.record["status"] = "open"
    with pytest.raises(ValueError, match="locked round"):
        make_duplicate_review(
            "week1", round_record=backend.record, submissions=backend.submissions,
            linked_pairs=[],
        )
    backend.record["status"] = "locked"
    backend.submissions[1]["qualified"] = False
    with pytest.raises(ValueError, match="ineligible"):
        make_duplicate_review(
            "week1", round_record=backend.record, submissions=backend.submissions,
            linked_pairs=[("a", "b", "same code")],
        )


def test_review_can_bind_near_copy_to_frozen_incumbent():
    backend = Backend()
    backend.record["incumbent_hotkey"] = "king"
    review = make_duplicate_review(
        "week1", round_record=backend.record, submissions=backend.submissions,
        linked_pairs=[("king", "a", "reviewed near copy")],
    )
    assert verify_duplicate_review(
        review, round_id="week1", round_record=backend.record,
        submissions=backend.submissions,
    ) == [("a", "king", "reviewed near copy")]
    backend.record["baseline_package_sha256"] = "8" * 64
    with pytest.raises(ValueError, match="locked intake"):
        verify_duplicate_review(
            review, round_id="week1", round_record=backend.record,
            submissions=backend.submissions,
        )
