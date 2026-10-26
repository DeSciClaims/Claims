from datetime import UTC, datetime, timedelta
from hashlib import sha256

from competition.production_handoff import FilesystemProductionReleaseStore, ProductionHandoffPlan
from competition.production_reader import ProductionReleaseReader
from competition.production_service import ProductionHandoffService


def _round(round_id, monday):
    return {"round_id": round_id, "network": "testnet", "status": "published",
            "deadline_at": monday.isoformat()}


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

    def service():
        return ProductionHandoffService(
            backend=Backend(), network="testnet", expected_signer_hotkey="signer",
            sealed_packages=object(), releases=store, reader=reader,
            publication_for_round=lambda _: {}, reference_upload_for_round=lambda _: None,
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
    root = tmp_path / "production"
    service = ProductionHandoffService(
        backend=Backend(), network="testnet", expected_signer_hotkey="signer",
        sealed_packages=object(), releases=FilesystemProductionReleaseStore(root),
        reader=ProductionReleaseReader(root),
        publication_for_round=lambda _: {}, reference_upload_for_round=lambda _: None,
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
    root = tmp_path / "production"
    service = ProductionHandoffService(
        backend=Backend(), network="testnet", expected_signer_hotkey="signer",
        sealed_packages=object(), releases=FilesystemProductionReleaseStore(root),
        reader=ProductionReleaseReader(root),
        publication_for_round=lambda _: {}, reference_upload_for_round=lambda _: None,
    )
    assert service.run_once(now=monday + timedelta(days=7)) == [
        {"round_id": "week1", "status": "error", "error_type": "ValueError"},
    ]
