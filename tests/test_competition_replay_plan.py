"""Second-validator jobs come from verified packages and an independent draw."""

import hashlib

import pytest

from competition.commitments import manifest_id
from competition.duplicates import select_distinct_submissions
from competition.drand import VerifiedBeacon
from competition.policy import commitment_hash
from competition.publication import canonical_sha256
from competition.replay_plan import build_replay_initial_plan
from competition.sample_release import PaperRecord, frozen_pool_hash, sample_receipt


PUBLIC_KEY = b"k" * 32
BASELINE = b"reference package"
PACKAGES = {"miner-a": b"package A", "miner-b": b"package B"}
PAPER_POOL = [PaperRecord("paper-1", "c" * 64)]
SIGNATURE = b"x" * 96
BEACON = VerifiedBeacon(
    42, hashlib.sha256(SIGNATURE).digest(), SIGNATURE.hex(),
    "d" * 64, "e" * 96, 123456,
)


def _upload(hotkey, package):
    salt = bytes([len(hotkey)]) * 32
    return {
        "hotkey": hotkey, "team_id": "team-" + hotkey,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "package_object_key": "sealed/" + hotkey,
        "package_size_bytes": len(package),
        "manifest": {"team_id": "team-" + hotkey},
        "salt_hex": salt.hex(),
        "commitment": commitment_hash(package, PUBLIC_KEY, salt),
        "commit_block": 90, "status": "accepted",
        "duplicate_of_hotkey": None,
    }


def _snapshot():
    configuration = {"pilot": True}
    draw = sample_receipt(
        papers=PAPER_POOL, beacon=BEACON, prescreen_count=0,
        screen_count=0, final_count=1, week_one=True,
    )
    snapshot = {
        "schema_version": "claims_competition_replay_snapshot_v1",
        "round_id": "week-1", "network": "testnet",
        "configuration_sha256": canonical_sha256(configuration),
        "deadline_block": 100, "reveal_block": 700,
        "archive_block_hash": "0x" + "a" * 64,
        **{key: value for key, value in draw.items() if key != "papers"},
        "paper_pool_sha256": frozen_pool_hash(PAPER_POOL),
        "duplicate_review_links": [],
        "duplicate_review_sha256": canonical_sha256([]),
        "baseline_package_sha256": hashlib.sha256(BASELINE).hexdigest(),
        "reference_package_sha256": hashlib.sha256(BASELINE).hexdigest(),
        "reference_release_id": "release-1",
        "incumbent_hotkey": None,
        "sample": draw["papers"],
        "stages": [],
        "uploads": [_upload(hotkey, package) for hotkey, package in PACKAGES.items()],
    }
    rules = {
        "schema_version": "claims_competition_rules_v1", "configuration": configuration,
        **{key: snapshot[key] for key in (
            "round_id", "network", "deadline_block", "reveal_block",
            "configuration_sha256", "reference_release_id", "incumbent_hotkey",
            "baseline_package_sha256", "reference_package_sha256",
        )},
    }
    snapshot["rules_public"] = rules
    snapshot["rules_sha256"] = canonical_sha256(rules)
    return snapshot


class Archive:
    def get_block_hash(self, block):
        assert block == 100
        return "0x" + "a" * 64

    def get_commitment_metadata(self, netuid, hotkey, block=None):
        assert (netuid, block) == (111, 100)
        upload = next(row for row in _snapshot()["uploads"] if row["hotkey"] == hotkey)
        return {"block": 90, "info": {"fields": [
            {"Sha256": upload["commitment"]},
            {"Raw": manifest_id(upload["manifest"]).encode().hex()},
        ]}}


def _package(upload):
    return PACKAGES.get(upload["hotkey"], BASELINE)


def _plan(snapshot=None, package_for_upload=_package, archive=None):
    return build_replay_initial_plan(
        snapshot or _snapshot(), archive=archive or Archive(), netuid=111,
        paper_pool=PAPER_POOL, verified_beacon=BEACON,
        package_for_upload=package_for_upload,
        hotkey_public_key=lambda _: PUBLIC_KEY,
        baseline_upload={
            "hotkey": "reference", "team_id": "claims-reference",
            "package_sha256": hashlib.sha256(BASELINE).hexdigest(),
            "package_size_bytes": len(BASELINE),
        },
    )


def test_replay_initial_plan_rechecks_archive_and_builds_own_job_grid():
    plan = _plan()
    assert plan.stage == "week_one"
    assert plan.selected_hotkeys == ("miner-a", "miner-b")
    assert plan.duplicate_verdicts == ()
    assert [entry["hotkey"] for entry in plan.entries] == ["reference", "miner-a", "miner-b"]
    assert {job["hotkey"] for job in plan.jobs} == {"reference", "miner-a", "miner-b"}
    assert {job["paper_id"] for job in plan.jobs} == {"paper-1"}


def test_replay_initial_plan_rejects_wrong_bytes_or_archive_block():
    with pytest.raises(ValueError, match="sealed package"):
        _plan(package_for_upload=lambda upload: b"altered" if upload["hotkey"] == "miner-a"
              else _package(upload))

    class WrongArchive(Archive):
        def get_block_hash(self, block):
            return "0x" + "f" * 64

    with pytest.raises(ValueError, match="archive block"):
        _plan(archive=WrongArchive())


def test_replay_initial_plan_rejects_primary_duplicate_mismatch():
    snapshot = _snapshot()
    snapshot["uploads"][0]["status"] = "duplicate"
    snapshot["uploads"][0]["duplicate_of_hotkey"] = "miner-b"
    with pytest.raises(ValueError, match="duplicate verdict differs"):
        _plan(snapshot)


def test_replay_initial_plan_rejects_changed_paper_draw():
    snapshot = _snapshot()
    snapshot["sample"][0]["judge_id"] = "J2" if snapshot["sample"][0]["judge_id"] == "J1" else "J1"
    with pytest.raises(ValueError, match="independently redrawn"):
        _plan(snapshot)


def test_replay_initial_plan_reproduces_exact_duplicate_lottery():
    snapshot = _snapshot()
    duplicate_package = PACKAGES["miner-a"]
    uploads = [_upload(hotkey, duplicate_package) for hotkey in PACKAGES]
    selection = select_distinct_submissions(
        uploads, seed=BEACON.seed,
        package_for_submission=lambda _: duplicate_package,
    )
    assert len(selection.selected_hotkeys) == 1
    chosen = selection.selected_hotkeys[0]
    for upload in uploads:
        if upload["hotkey"] != chosen:
            upload["status"] = "duplicate"
            upload["duplicate_of_hotkey"] = chosen
    snapshot["uploads"] = uploads

    class DuplicateArchive(Archive):
        def get_commitment_metadata(self, netuid, hotkey, block=None):
            upload = next(row for row in uploads if row["hotkey"] == hotkey)
            return {"block": 90, "info": {"fields": [
                {"Sha256": upload["commitment"]},
                {"Raw": manifest_id(upload["manifest"]).encode().hex()},
            ]}}

    plan = _plan(
        snapshot, package_for_upload=lambda upload: (
            duplicate_package if upload["hotkey"] in PACKAGES else BASELINE
        ), archive=DuplicateArchive(),
    )
    assert plan.selected_hotkeys == (chosen,)
    assert len(plan.duplicate_verdicts) == 1
    assert {job["hotkey"] for job in plan.jobs} == {"reference", chosen}
