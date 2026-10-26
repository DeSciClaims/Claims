"""Independent replay service polls local roles without exposing private data."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from competition import replay_pilot_factory
from competition.replay_pilot_factory import _private_receipts
from neurons import competition_replay_service


def test_replay_service_runs_controller_and_worker_once(monkeypatch, capsys, tmp_path):
    calls = []

    def controller(round_id, backend_url, db_path):
        calls.append(("controller", round_id, backend_url, db_path))
        return SimpleNamespace(run_once=lambda: {
            "status": "waiting_for_scores", "private_gold": "never-print",
        })

    def worker(round_id, backend_url, db_path, worker_id):
        calls.append(("worker", round_id, backend_url, db_path, worker_id))
        return SimpleNamespace(run_once=lambda: {"status": "completed", "secret": "never-print"})

    monkeypatch.setattr(competition_replay_service, "_factory", lambda _: SimpleNamespace(
        create_replay_controller=controller, create_replay_worker=worker,
    ))
    common = ["--round-id", "week-1", "--backend-url", "https://backend.example",
              "--db-path", str(tmp_path / "replay.sqlite"), "--once"]
    assert competition_replay_service.main(["--role", "controller", *common]) == 0
    assert competition_replay_service.main([
        "--role", "worker", "--worker-id", "worker-1", *common,
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["status"] for row in rows] == ["waiting_for_scores", "completed"]
    assert all(set(row) == {"round_id", "role", "status"} for row in rows)
    assert calls == [
        ("controller", "week-1", "https://backend.example", tmp_path / "replay.sqlite"),
        ("worker", "week-1", "https://backend.example", tmp_path / "replay.sqlite",
         "worker-1"),
    ]


def test_replay_service_redacts_errors(monkeypatch, capsys, tmp_path):
    def fail():
        raise RuntimeError("provider answer and key: private")

    monkeypatch.setattr(competition_replay_service, "_factory", lambda _: SimpleNamespace(
        create_replay_controller=lambda *_: SimpleNamespace(run_once=fail),
    ))
    assert competition_replay_service.main([
        "--role", "controller", "--round-id", "week-1",
        "--backend-url", "https://backend.example",
        "--db-path", str(tmp_path / "replay.sqlite"), "--once",
    ]) == 1
    assert json.loads(capsys.readouterr().err) == {
        "round_id": "week-1", "role": "controller", "status": "error",
        "error_type": "RuntimeError",
    }


def test_replay_factory_requires_private_baseline_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIMS_COMPETITION_REPLAY_ROUNDS_DIR", str(tmp_path))
    path = tmp_path / "week-1.json"
    path.write_text(json.dumps({
        "round_id": "week-1",
        "baseline_upload": {"hotkey": "reference", "package_sha256": "a" * 64},
        "reference_upload": None,
    }))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        _private_receipts("week-1")
    path.chmod(0o600)
    baseline, reference = _private_receipts("week-1")
    assert baseline["hotkey"] == "reference" and reference is None


def test_replay_factory_uses_read_only_token_and_separate_log_bucket(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIMS_COMPETITION_REPLAY_TOKEN", "replay-only")
    monkeypatch.setenv("CLAIMS_HIPPIUS_ENDPOINT", "https://s3.hippius.com")
    monkeypatch.setenv("CLAIMS_HIPPIUS_ACCESS_KEY_ID", "read-key")
    monkeypatch.setenv("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("CLAIMS_HIPPIUS_PACKAGE_BUCKET", "sealed-packages")
    monkeypatch.setenv("CLAIMS_HIPPIUS_REPLAY_LOG_BUCKET", "replay-logs")
    monkeypatch.setenv("CLAIMS_ARCHIVE_SUBTENSOR_NETWORK", "test")
    monkeypatch.setenv("CLAIMS_COMPETITION_NETUID", "111")
    baseline = {"hotkey": "reference", "package_sha256": "a" * 64}
    monkeypatch.setattr(replay_pilot_factory, "_private_receipts", lambda _: (baseline, None))
    seen = {}

    def replay_reader(base_url, token):
        seen["backend_credentials"] = (base_url, token)
        return "read-only-reader"

    def storage(client, package_bucket, log_bucket):
        seen["buckets"] = (package_bucket, log_bucket)
        return "private-storage"

    monkeypatch.setattr(replay_pilot_factory, "CompetitionReplayInputs", replay_reader)
    monkeypatch.setattr(replay_pilot_factory, "hippius_s3_client", lambda **_: "s3-client")
    monkeypatch.setattr(replay_pilot_factory, "HippiusPrivateStorage", storage)
    monkeypatch.setattr(replay_pilot_factory, "BittensorArchive", lambda _: "archive")
    monkeypatch.setattr(replay_pilot_factory, "ReplayStore", lambda path: "local-store")
    bittensor = ModuleType("bittensor")
    bittensor.Subtensor = lambda network: network
    wallet = ModuleType("bittensor_wallet")
    wallet.Keypair = lambda ss58_address: SimpleNamespace(public_key=ss58_address.encode())
    monkeypatch.setitem(sys.modules, "bittensor", bittensor)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", wallet)

    def bootstrap(round_id, **kwargs):
        seen["bootstrap"] = (round_id, kwargs)
        return SimpleNamespace(snapshot={
            "incumbent_hotkey": None, "reference_package_sha256": "a" * 64,
        })

    monkeypatch.setattr(replay_pilot_factory, "initialize_replay_runtime", bootstrap)
    backend, private_storage, returned_baseline, reference = replay_pilot_factory._bootstrap(
        "week-1", "https://backend.example", tmp_path / "replay.sqlite",
    )
    assert backend.snapshot["incumbent_hotkey"] is None
    assert (private_storage, returned_baseline, reference) == (
        "private-storage", baseline, None,
    )
    assert seen["backend_credentials"] == ("https://backend.example", "replay-only")
    assert seen["buckets"] == ("sealed-packages", "replay-logs")
    assert seen["bootstrap"][1]["hotkey_public_key"]("miner-hotkey") == b"miner-hotkey"
    assert seen["bootstrap"][1]["store"] == "local-store"
