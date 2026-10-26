from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib

import pytest
from bittensor_wallet import Keypair

from competition.production_handoff import (
    FilesystemProductionReleaseStore, activate_verified_handoff, planned_production_handoff,
)
from competition.handoff_controller import activate_published_round
from competition.publication import canonical_sha256, publication_bundle, sign_publication


PACKAGE = b"sealed-production-package"
PACKAGE_SHA = hashlib.sha256(PACKAGE).hexdigest()
MANIFEST = {"schema_version": "claims_competition_manifest_v1",
            "entrypoint": "claims_sample_miner.cli:main", "models": ["example/model"],
            "package_filename": "claims_sample_miner-0.1.0-py3-none-any.whl"}


def _fixture(decision="new_crown"):
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    round_record = {
        "round_id": "week2", "network": "testnet", "status": "published",
        "deadline_at": "2026-10-26T12:00:00+00:00", "incumbent_hotkey": "incumbent",
        "archive_block_hash": "0x" + "a" * 64, "drand_round": 10,
        "drand_seed": "b" * 64, "drand_chain_hash": "c" * 64,
        "drand_public_key": "d" * 96, "drand_signature": "e" * 192,
        "drand_scheduled_at": 1_800_000_000, "paper_pool_sha256": "f" * 64,
        "rules_sha256": "1" * 64, "configuration_sha256": "2" * 64,
        "baseline_package_sha256": "3" * 64,
        "reference_package_sha256": "4" * 64,
    }
    rules = {"round_id": "week2", "network": "testnet",
             "deadline_at": round_record["deadline_at"]}
    round_record["rules_public"] = rules
    round_record["rules_sha256"] = canonical_sha256(rules)
    stage = {"stage": "final", "entries": [
        {"hotkey": "winner", "role": "challenger", "team_id": "new-team",
         "package_sha256": PACKAGE_SHA},
        {"hotkey": "incumbent", "role": "incumbent", "team_id": "old-team",
         "package_sha256": "3" * 64},
    ]}
    bundle = publication_bundle(round_record, results={
        "decision": decision,
        "production_hotkey": "winner" if decision == "new_crown" else "incumbent",
        "winner_hotkey": "winner" if decision == "new_crown" else None,
        "final_pair_scores": [{
            "round_id": "week2", "stage": "final", "paper_id": "paper-1",
            "challenger_hotkey": "winner", "challenger_package_sha256": PACKAGE_SHA,
            "configuration_sha256": "2" * 64,
        }],
    }, weights={
        "vector": [{"uid": 0, "weight": 0.03}, {"uid": 1, "weight": 0.07},
                   {"uid": 9, "weight": 0.90}],
        "roles": [
            {"hotkey": "operator", "role": "operator", "share": 0.90},
            {"hotkey": "winner" if decision == "new_crown" else "incumbent",
             "role": "production", "share": 0.07},
            {"hotkey": "", "role": "runner_up_1_burned", "share": 0.02},
            {"hotkey": "", "role": "runner_up_2_burned", "share": 0.01},
        ],
    })
    return signer, round_record, stage, sign_publication(bundle, signer)


def _plan(signer, round_record, stage, receipt, now):
    return planned_production_handoff(
        receipt=receipt, round_record=round_record, final_stage=stage,
        expected_signer_hotkey=signer.ss58_address, network="testnet", now=now,
    )


def test_new_crown_waits_until_monday_and_binds_exact_package():
    signer, round_record, stage, receipt = _fixture()
    assert _plan(signer, round_record, stage, receipt,
                 datetime(2026, 11, 2, 8, 59, tzinfo=UTC)) is None
    plan = _plan(signer, round_record, stage, receipt,
                 datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert plan.hotkey == "winner"
    assert plan.package_sha256 == PACKAGE_SHA
    assert plan.receipt_sha256 == receipt["bundle_sha256"]


def test_continuing_incumbent_has_no_handoff():
    signer, round_record, stage, receipt = _fixture("incumbent_continues")
    assert _plan(signer, round_record, stage, receipt,
                 datetime(2026, 11, 2, 9, 0, tzinfo=UTC)) is None


def test_rejects_package_or_frozen_round_change():
    signer, round_record, stage, receipt = _fixture()
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    stage["entries"][0]["package_sha256"] = "5" * 64
    with pytest.raises(ValueError, match="signed final paper grid"):
        _plan(signer, round_record, stage, receipt, now)
    stage["entries"][0]["package_sha256"] = PACKAGE_SHA
    round_record["configuration_sha256"] = "6" * 64
    with pytest.raises(ValueError, match="signed publication"):
        _plan(signer, round_record, stage, receipt, now)


def test_handoff_gate_cannot_move_after_rules_are_signed():
    signer, round_record, stage, receipt = _fixture()
    round_record["deadline_at"] = "2026-11-02T12:00:00+00:00"
    with pytest.raises(ValueError, match="signed rules"):
        _plan(signer, round_record, stage, receipt,
              datetime(2026, 11, 2, 9, tzinfo=UTC))


def test_verified_handoff_writes_atomic_pointer_and_rejects_stale_replay(tmp_path):
    signer, round_record, stage, receipt = _fixture()
    store = FilesystemProductionReleaseStore(tmp_path / "production")
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    kwargs = {
        "receipt": receipt, "round_record": round_record, "final_stage": stage,
        "expected_signer_hotkey": signer.ss58_address, "network": "testnet", "now": now,
        "releases": store,
    }
    assert activate_verified_handoff(
        **{**kwargs, "now": datetime(2026, 11, 2, 8, 59, tzinfo=UTC)},
        fetch_package=lambda _: pytest.fail("package fetched before activation gate"),
    ) is None
    active = activate_verified_handoff(**kwargs, fetch_package=lambda _: PACKAGE)
    assert active["package_sha256"] == PACKAGE_SHA
    assert (store.root / f"{PACKAGE_SHA}.zip").read_bytes() == PACKAGE
    assert activate_verified_handoff(**kwargs, fetch_package=lambda _: PACKAGE) == active
    with pytest.raises(ValueError, match="package or receipt"):
        activate_verified_handoff(**kwargs, fetch_package=lambda _: b"wrong package")
    first_plan = _plan(signer, round_record, stage, receipt, now)
    newer = replace(first_plan, receipt_sha256="a" * 64,
                    activate_at=first_plan.activate_at + timedelta(days=7))
    later = now + timedelta(days=7)
    second_active = store.activate(newer, PACKAGE, now=later)
    assert second_active["previous"] == {key: value for key, value in active.items()
                                         if key != "previous"}
    with pytest.raises(ValueError, match="older"):
        store.activate(first_plan, PACKAGE, now=later)
    restored = store.rollback(
        expected_receipt_sha256=newer.receipt_sha256,
        reason="production quality breach", now=later + timedelta(hours=1),
    )
    assert restored["receipt_sha256"] == first_plan.receipt_sha256
    assert restored["rolled_back_from"] == newer.receipt_sha256
    assert store.rollback(
        expected_receipt_sha256=newer.receipt_sha256,
        reason="production quality breach", now=later + timedelta(hours=1),
    ) == restored
    with pytest.raises(ValueError, match="older"):
        store.activate(newer, PACKAGE, now=later + timedelta(hours=2))


def test_first_release_cannot_roll_back_without_previous_package(tmp_path):
    signer, round_record, stage, receipt = _fixture()
    store = FilesystemProductionReleaseStore(tmp_path / "production")
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    plan = _plan(signer, round_record, stage, receipt, now)
    store.activate(plan, PACKAGE, now=now)
    with pytest.raises(ValueError, match="no previous release"):
        store.rollback(expected_receipt_sha256=receipt["bundle_sha256"],
                       reason="breach", now=now + timedelta(hours=1))


def test_handoff_controller_resolves_only_winning_sealed_upload(tmp_path):
    signer, round_record, stage, receipt = _fixture()

    class Backend:
        def get_round(self, _):
            return round_record

        def list_stages(self, _):
            return [stage]

        def list_submissions(self, _):
            return [
                {"hotkey": "runner", "package_sha256": "b" * 64, "status": "accepted"},
                {"hotkey": "winner", "package_sha256": PACKAGE_SHA,
                 "status": "accepted", "package_object_key": "sealed/winner.zip",
                 "manifest": MANIFEST},
            ]

    class Storage:
        seen = None

        def package_for_submission(self, upload):
            self.seen = upload["package_object_key"]
            return PACKAGE

    storage = Storage()
    store = FilesystemProductionReleaseStore(tmp_path / "production")
    kwargs = {
        "backend": Backend(), "publication_for_round": lambda _: receipt,
        "sealed_packages": storage, "releases": store,
        "expected_signer_hotkey": signer.ss58_address, "network": "testnet",
    }
    assert activate_published_round(
        "week2", **kwargs, now=datetime(2026, 11, 2, 8, 59, tzinfo=UTC),
    ) is None
    assert storage.seen is None
    active = activate_published_round(
        "week2", **kwargs, now=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
    )
    assert active["package_sha256"] == PACKAGE_SHA
    assert active["manifest"] == MANIFEST
    assert storage.seen == "sealed/winner.zip"


def test_failed_incumbent_hands_production_to_unpaid_reference(tmp_path):
    signer, round_record, _, _ = _fixture()
    reference_bytes = b"sealed-reference-package"
    reference_hash = hashlib.sha256(reference_bytes).hexdigest()
    round_record["reference_package_sha256"] = reference_hash
    stage = {"stage": "final", "entries": [
        {"hotkey": "team-reference", "role": "reference",
         "team_id": "claims-reference", "package_sha256": reference_hash},
        {"hotkey": "winner", "role": "challenger",
         "team_id": "new-team", "package_sha256": PACKAGE_SHA},
    ]}
    bundle = publication_bundle(round_record, results={
        "decision": "reference_fallback", "production_hotkey": None,
        "fallback_reference_hotkey": "team-reference",
        "worker_security_audit": {"team-reference": {"passed": True}},
        "final_pair_scores": [{
            "round_id": "week2", "stage": "final", "paper_id": "paper-1",
            "baseline_hotkey": "team-reference",
            "baseline_package_sha256": reference_hash,
            "challenger_hotkey": "winner", "challenger_package_sha256": PACKAGE_SHA,
        }],
    }, weights={
        "vector": [{"uid": 0, "weight": 0.10}, {"uid": 9, "weight": 0.90}],
        "roles": [
            {"hotkey": "operator", "role": "operator", "share": 0.90},
            {"hotkey": "", "role": "production_burned", "share": 0.07},
            {"hotkey": "", "role": "runner_up_1_burned", "share": 0.02},
            {"hotkey": "", "role": "runner_up_2_burned", "share": 0.01},
        ],
    })
    receipt = sign_publication(bundle, signer)
    plan = _plan(signer, round_record, stage, receipt,
                 datetime(2026, 11, 2, 9, tzinfo=UTC))
    assert plan.hotkey == "team-reference"
    assert plan.package_sha256 == reference_hash

    class Backend:
        def get_round(self, _):
            return round_record

        def list_stages(self, _):
            return [{"stage": "screen", "entries": []},
                    {"stage": "rerun", "source_stage": "screen", "entries": []}, stage]

        def list_submissions(self, _):
            return []

    class Storage:
        def package_for_submission(self, upload):
            assert upload["package_object_key"] == "sealed/reference.zip"
            return reference_bytes

    active = activate_published_round(
        "week2", backend=Backend(), publication_for_round=lambda _: receipt,
        sealed_packages=Storage(),
        releases=FilesystemProductionReleaseStore(tmp_path / "production"),
        expected_signer_hotkey=signer.ss58_address, network="testnet",
        now=datetime(2026, 11, 2, 9, tzinfo=UTC),
        reference_upload={"hotkey": "team-reference", "package_sha256": reference_hash,
                          "package_object_key": "sealed/reference.zip", "manifest": MANIFEST},
    )
    assert active["hotkey"] == "team-reference"
    assert active["package_sha256"] == reference_hash


def test_week_one_no_crown_activates_reference_package(tmp_path):
    signer, round_record, _, _ = _fixture()
    reference_bytes = b"first-week-reference"
    reference_hash = hashlib.sha256(reference_bytes).hexdigest()
    round_record.update({"incumbent_hotkey": None,
                         "baseline_package_sha256": reference_hash,
                         "reference_package_sha256": reference_hash})
    stage = {"stage": "week_one", "entries": [
        {"hotkey": "reference", "role": "reference", "team_id": "claims",
         "package_sha256": reference_hash},
        {"hotkey": "miner", "role": "challenger", "team_id": "other",
         "package_sha256": PACKAGE_SHA},
    ]}
    receipt = sign_publication(publication_bundle(round_record, results={
        "decision": "no_crown", "production_hotkey": None,
        "worker_security_audit": {"reference": {"passed": True}},
        "final_pair_scores": [{
            "stage": "week_one", "paper_id": "paper-1",
            "baseline_hotkey": "reference",
            "baseline_package_sha256": reference_hash,
            "challenger_hotkey": "miner", "challenger_package_sha256": PACKAGE_SHA,
        }],
    }, weights={"vector": [{"uid": 0, "weight": 0.10},
                           {"uid": 9, "weight": 0.90}],
                "roles": [
                    {"hotkey": "operator", "role": "operator", "share": 0.90},
                    {"hotkey": "", "role": "production_burned", "share": 0.07},
                    {"hotkey": "", "role": "runner_up_1_burned", "share": 0.02},
                    {"hotkey": "", "role": "runner_up_2_burned", "share": 0.01},
                ]}
    ), signer)
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    plan = _plan(signer, round_record, stage, receipt, now)
    assert (plan.hotkey, plan.package_sha256) == ("reference", reference_hash)

    class Backend:
        def get_round(self, _):
            return round_record

        def list_stages(self, _):
            return [stage]

        def list_submissions(self, _):
            return []

    class Storage:
        def package_for_submission(self, upload):
            assert upload["package_sha256"] == reference_hash
            return reference_bytes

    active = activate_published_round(
        "week2", backend=Backend(), publication_for_round=lambda _: receipt,
        sealed_packages=Storage(),
        releases=FilesystemProductionReleaseStore(tmp_path / "production"),
        expected_signer_hotkey=signer.ss58_address, network="testnet", now=now,
        reference_upload={"hotkey": "reference", "package_sha256": reference_hash,
                          "manifest": MANIFEST},
    )
    assert active["hotkey"] == "reference"
