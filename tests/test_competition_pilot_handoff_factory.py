import json

from competition.pilot_handoff_factory import create_handoff_service


def test_handoff_factory_binds_first_week_reference_to_sealed_baseline(tmp_path, monkeypatch):
    rounds = tmp_path / "rounds"
    rounds.mkdir()
    (rounds / "week1.json").write_text(json.dumps({
        "round_id": "week1", "configuration_sha256": "a" * 64,
        "baseline_upload": {"hotkey": "reference", "package_sha256": "b" * 64,
                            "package_object_key": "sealed/reference"},
        "reference_upload": None,
    }))
    for name, value in {
        "CLAIMS_COMPETITION_ROUNDS_DIR": str(rounds),
        "CLAIMS_COMPETITION_PRODUCTION_ROOT": str(tmp_path / "production"),
        "CLAIMS_COMPETITION_PUBLISHER_HOTKEY": "publisher",
        "CLAIMS_HIPPIUS_ENDPOINT": "https://s3.hippius.example",
        "CLAIMS_HIPPIUS_ACCESS_KEY_ID": "access",
        "CLAIMS_HIPPIUS_SECRET_ACCESS_KEY": "secret",
        "CLAIMS_HIPPIUS_PACKAGE_BUCKET": "packages",
        "CLAIMS_HIPPIUS_LOG_BUCKET": "logs",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("competition.pilot_handoff_factory.hippius_s3_client",
                        lambda **_: object())

    class Backend:
        def get_round(self, round_id):
            return {"round_id": round_id, "network": "testnet",
                    "configuration_sha256": "a" * 64,
                    "reference_package_sha256": "b" * 64,
                    "incumbent_hotkey": None}

    service = create_handoff_service(Backend(), "https://backend.example", "testnet")
    assert service.reference_upload_for_round("week1")["hotkey"] == "reference"
    assert service.reader.read_active() is None
