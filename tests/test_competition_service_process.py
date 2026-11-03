import sys
from types import ModuleType, SimpleNamespace

from neurons import competition_service


def test_slow_scoring_does_not_block_intake_or_recurrence(monkeypatch,tmp_path):
    import json
    import threading
    import time
    module=ModuleType('competition.fake_overlap_factory')
    module.create_controller_inputs=lambda *_:None
    module.create_intake_inputs=lambda *_:None
    module.prepare_evaluation=lambda *_:{'status':'evaluation_ready'}
    monkeypatch.setitem(sys.modules,module.__name__,module)
    monkeypatch.setenv('CLAIMS_COMPETITION_CONTROLLER_TOKEN','private-token')
    monkeypatch.setattr(competition_service,'CompetitionBackend',lambda **_:object())
    ticks=[];intakes=[];release=threading.Event();started=threading.Event()
    scheduler=SimpleNamespace(tick=lambda:ticks.append(1) or None)
    monkeypatch.setattr('competition.pilot_recurrence_factory.create_round_scheduler',lambda *a,**kw:scheduler)
    monkeypatch.setattr(competition_service,'run_intake_cycle',lambda **kw:intakes.append(1) or [])
    def slow(**kw):
        started.set()
        assert release.wait(2)
        return []
    monkeypatch.setattr(competition_service,'run_controller_cycle',slow)
    original_sleep=time.sleep
    def sleep(_):
        assert started.wait(1)
        if len(intakes)>=3:
            release.set()
            raise KeyboardInterrupt
        original_sleep(.01)
    monkeypatch.setattr(competition_service.time,'sleep',sleep)
    profile=tmp_path/'recurrence.json';profile.write_text(json.dumps({'independent_intake':True}))
    assert competition_service.main(['--role','controller','--network','testnet',
        '--backend-url','https://backend.example','--factory-module',module.__name__,
        '--recurrence-config',str(profile)])==0
    assert len(intakes)==len(ticks)==3


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


def test_production_cli_keeps_private_extraction_out_of_journal(monkeypatch, capsys):
    module = ModuleType("competition.fake_production_factory")
    module.create_production_worker = lambda backend, **kwargs: SimpleNamespace(
        run_once=lambda: {"status": "completed", "artifact": {
            "private_claim": "do not log this"}},
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "private-token")
    assert competition_service.main([
        "--role", "production", "--network", "testnet",
        "--backend-url", "https://backend.example",
        "--factory-module", module.__name__,
        "--worker-id", "production-worker", "--once",
    ]) == 0
    output = capsys.readouterr()
    assert '"job_status": "completed"' in output.out
    assert "do not log this" not in output.out + output.err
