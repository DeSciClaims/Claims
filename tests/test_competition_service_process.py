import sys
from types import ModuleType, SimpleNamespace

from neurons import competition_service


def test_controller_cli_runs_one_cycle_without_exposing_private_data(monkeypatch, capsys):
    module = ModuleType("competition.fake_service_factory")
    module.create_controller_inputs = lambda row, backend: SimpleNamespace(
        evaluation=SimpleNamespace(round_id=row["round_id"],
                                   configuration_sha256=row["configuration_sha256"]),
        paper_inputs=SimpleNamespace(backend=backend),
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "private-token")

    class Backend:
        def __init__(self, **kwargs):
            assert kwargs["controller_token"] == "private-token"

        def list_pending_rounds(self, network):
            return [{"round_id": "week1", "network": network, "status": "open",
                     "configuration_sha256": "a" * 64}]

    monkeypatch.setattr(competition_service, "CompetitionBackend", Backend)
    monkeypatch.setattr("competition.service_loops.tick_round",
                        lambda *args, **kwargs: {"status": "waiting_for_deadline"})
    assert competition_service.main([
        "--role", "controller", "--network", "testnet",
        "--backend-url", "https://backend.example",
        "--factory-module", module.__name__, "--once",
    ]) == 0
    output = capsys.readouterr()
    assert '"status": "waiting_for_deadline"' in output.out
    assert "private-token" not in output.out + output.err


def test_worker_cli_requires_identity_and_trusted_factory(monkeypatch):
    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "private-token")
    assert competition_service.main([
        "--role", "worker", "--network", "testnet",
        "--backend-url", "https://backend.example",
        "--factory-module", "untrusted.miner", "--worker-id", "worker-a", "--once",
    ]) == 1


def test_handoff_cli_uses_separate_factory_entrypoint(monkeypatch, capsys):
    module = ModuleType("competition.fake_handoff_factory")
    module.create_handoff_service = lambda backend, url, network: SimpleNamespace(
        run_once=lambda: [{"round_id": "week1", "status": "activated"}],
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "private-token")
    assert competition_service.main([
        "--role", "handoff", "--network", "testnet",
        "--backend-url", "https://backend.example",
        "--factory-module", module.__name__, "--once",
    ]) == 0
    assert '"status": "activated"' in capsys.readouterr().out
