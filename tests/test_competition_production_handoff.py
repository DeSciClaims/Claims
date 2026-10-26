from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib

import pytest
from bittensor_wallet import Keypair

from competition.production_handoff import (
    FilesystemProductionReleaseStore, activate_verified_handoff, planned_production_handoff,
)
from competition.publication import publication_bundle, sign_publication


PACKAGE = b"sealed-production-package"
PACKAGE_SHA = hashlib.sha256(PACKAGE).hexdigest()


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
    }
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
    assert second_active["previous"] == active
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
