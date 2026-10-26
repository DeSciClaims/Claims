from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from bittensor_wallet import Keypair

from competition.publication import canonical_sha256, publication_bundle, sign_publication
from competition.production_handoff import FilesystemProductionReleaseStore, ProductionHandoffPlan
from competition.production_reader import ProductionReleaseReader
from competition.production_service import ProductionHandoffService


def _round(round_id, monday):
    rules = {"round_id": round_id, "network": "testnet",
             "deadline_at": monday.isoformat()}
    return {"round_id": round_id, "network": "testnet", "status": "published",
            "deadline_at": monday.isoformat(), "rules_public": rules,
            "rules_sha256": canonical_sha256(rules)}


def test_handoff_scheduler_catches_up_and_preserves_rollback(tmp_path, monkeypatch):
    monday = datetime(2026, 10, 26, 12, tzinfo=UTC)
    now = monday + timedelta(days=14)
    gates = {"week1": monday + timedelta(days=7, hours=-3),
             "week2": monday + timedelta(days=14, hours=-3)}
    rows = [_round("week2", monday + timedelta(days=7)), _round("week1", monday)]

    class Backend:
        def list_published_rounds(self, network):
            assert network == "testnet"
            return rows

    root = tmp_path / "production"
    store = FilesystemProductionReleaseStore(root)
    reader = ProductionReleaseReader(root)
    package = b"verified-package"
    digest = sha256(package).hexdigest()
    calls = []

    def activate(round_id, **kwargs):
        calls.append(round_id)
        return store.activate(ProductionHandoffPlan(
            round_id, ("a" if round_id == "week1" else "b") * 64,
            "miner", digest, gates[round_id],
        ), package, now=kwargs["now"])

    monkeypatch.setattr("competition.production_service.activate_published_round", activate)
    monkeypatch.setattr("competition.production_service.verify_weekly_receipt",
                        lambda receipt, **_: receipt["bundle"])

    def service():
        return ProductionHandoffService(
            backend=Backend(), network="testnet", expected_signer_hotkey="signer",
            sealed_packages=object(), releases=store, reader=reader,
            publication_for_round=lambda rid: {"bundle": {
                "round_id": rid, "rules_sha256": next(row["rules_sha256"]
                                                       for row in rows if row["round_id"] == rid),
            }}, reference_upload_for_round=lambda _: None,
        )

    first = service()
    assert [row["status"] for row in first.run_once(now=now)] == ["activated", "activated"]
    assert calls == ["week1", "week2"]
    assert first.run_once(now=now) == []
    assert service().run_once(now=now) == []
    store.rollback(expected_receipt_sha256="b" * 64, reason="breach", now=now)
    assert reader.read_active().latest_activation_at == gates["week2"]
    assert service().run_once(now=now) == []
    assert calls == ["week1", "week2"]


def test_handoff_scheduler_stops_on_first_error(tmp_path, monkeypatch):
    monday = datetime(2026, 10, 26, 12, tzinfo=UTC)

    class Backend:
        def list_published_rounds(self, _):
            return [_round("week1", monday),
                    _round("week2", monday + timedelta(days=7))]

    calls = []

    def fail(round_id, **_):
        calls.append(round_id)
        raise ValueError("private error text")

    monkeypatch.setattr("competition.production_service.activate_published_round", fail)
    monkeypatch.setattr("competition.production_service.verify_weekly_receipt",
                        lambda receipt, **_: receipt["bundle"])
    root = tmp_path / "production"
    service = ProductionHandoffService(
        backend=Backend(), network="testnet", expected_signer_hotkey="signer",
        sealed_packages=object(), releases=FilesystemProductionReleaseStore(root),
        reader=ProductionReleaseReader(root),
        publication_for_round=lambda rid: {"bundle": {
            "round_id": rid, "rules_sha256": canonical_sha256({
                "round_id": rid, "network": "testnet",
                "deadline_at": (monday if rid == "week1" else
                                monday + timedelta(days=7)).isoformat(),
            }),
        }}, reference_upload_for_round=lambda _: None,
    )
    assert service.run_once(now=monday + timedelta(days=14)) == [
        {"round_id": "week1", "status": "error", "error_type": "ValueError"},
    ]
    assert calls == ["week1"]


def test_no_change_needs_an_existing_active_release(tmp_path, monkeypatch):
    monday = datetime(2026, 10, 26, 12, tzinfo=UTC)

    class Backend:
        def list_published_rounds(self, _):
            return [_round("week1", monday)]

    monkeypatch.setattr("competition.production_service.activate_published_round",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("competition.production_service.verify_weekly_receipt",
                        lambda receipt, **_: receipt["bundle"])
    root = tmp_path / "production"
    service = ProductionHandoffService(
        backend=Backend(), network="testnet", expected_signer_hotkey="signer",
        sealed_packages=object(), releases=FilesystemProductionReleaseStore(root),
        reader=ProductionReleaseReader(root),
        publication_for_round=lambda rid: {"bundle": {
            "round_id": rid, "rules_sha256": _round(rid, monday)["rules_sha256"],
        }}, reference_upload_for_round=lambda _: None,
    )
    assert service.run_once(now=monday + timedelta(days=7)) == [
        {"round_id": "week1", "status": "error", "error_type": "ValueError"},
    ]


def test_scheduler_verifies_signature_before_using_activation_gate(tmp_path):
    monday = datetime(2026, 10, 26, 12, tzinfo=UTC)
    row = _round("week1", monday)
    row.update({
        "archive_block_hash": "0x" + "a" * 64,
        "drand_round": 1, "drand_seed": "b" * 64,
        "drand_chain_hash": "c" * 64, "drand_public_key": "d" * 96,
        "drand_signature": "e" * 192, "drand_scheduled_at": 1_800_000_000,
        "paper_pool_sha256": "f" * 64,
        "duplicate_review_sha256": "a" * 64,
        "configuration_sha256": "1" * 64,
        "baseline_package_sha256": "2" * 64,
        "reference_package_sha256": "3" * 64,
    })
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    receipt = sign_publication(publication_bundle(row, results={
        "decision": "incumbent_continues",
    }, weights={
        "vector": [{"uid": 0, "weight": 0.03}, {"uid": 1, "weight": 0.07},
                   {"uid": 9, "weight": 0.90}],
        "roles": [
            {"hotkey": "operator", "role": "operator", "share": 0.90},
            {"hotkey": "incumbent", "role": "production", "share": 0.07},
            {"hotkey": "", "role": "runner_up_1_burned", "share": 0.02},
            {"hotkey": "", "role": "runner_up_2_burned", "share": 0.01},
        ],
    }), signer)

    class Backend:
        def list_published_rounds(self, _):
            return [row]

    root = tmp_path / "production"
    service = ProductionHandoffService(
        backend=Backend(), network="testnet",
        expected_signer_hotkey=signer.ss58_address,
        sealed_packages=object(), releases=FilesystemProductionReleaseStore(root),
        reader=ProductionReleaseReader(root),
        publication_for_round=lambda _: receipt,
        reference_upload_for_round=lambda _: None,
    )
    assert service.run_once(now=monday) == []
    assert service._verified_gates["week1"][1] == monday + timedelta(days=7, hours=-3)
    bad = {**receipt, "signature_hex": "0" * 128}
    service.publication_for_round = lambda _: bad
    service._verified_gates.clear()
    with pytest.raises(ValueError, match="signature"):
        service.run_once(now=monday)
