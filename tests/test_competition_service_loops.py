from datetime import UTC, datetime
from types import SimpleNamespace

from competition.service_loops import run_controller_cycle, run_worker_cycle


def test_closed_rounds_evaluate_in_deadline_order_while_next_intake_stays_open(monkeypatch):
    rows=[{'round_id':'b','network':'testnet','status':'locked','deadline_at':'2026-11-04T12:00:00+00:00','configuration_sha256':'b'*64},
          {'round_id':'c','network':'testnet','status':'open','deadline_at':'2026-11-05T12:00:00+00:00','configuration_sha256':'c'*64},
          {'round_id':'a','network':'testnet','status':'final','deadline_at':'2026-11-03T12:00:00+00:00','configuration_sha256':'a'*64}]
    backend=SimpleNamespace(list_pending_rounds=lambda _:rows)
    seen=[]
    def inputs(row):
        seen.append(row['round_id'])
        return SimpleNamespace(evaluation=SimpleNamespace(round_id=row['round_id'],configuration_sha256=row['configuration_sha256']),paper_inputs=SimpleNamespace(backend=backend))
    monkeypatch.setattr('competition.service_loops.tick_round',lambda *a,**kw:{'status':'waiting_for_final_scores'})
    assert run_controller_cycle(backend=backend,network='testnet',inputs_for_round=inputs,separate_intake=True)==[
        {'round_id':'a','status':'waiting_for_final_scores'}]
    assert seen==['a']


def test_queued_round_waits_before_constructing_judges_or_worker_inputs():
    row={'round_id':'b','network':'testnet','status':'locked','deadline_at':'2026-11-04T12:00:00+00:00'}
    backend=SimpleNamespace(list_pending_rounds=lambda _:[row])
    assert run_controller_cycle(backend=backend,network='testnet',separate_intake=True,
        evaluation_preparer=lambda *_:{'status':'waiting_for_preceding_handoff'},
        inputs_for_round=lambda _: (_ for _ in ()).throw(AssertionError('paid adapters started early')))==[
            {'round_id':'b','status':'waiting_for_preceding_handoff'}]


def test_intake_locks_deadline_commitments_without_evaluation_adapters(monkeypatch):
    from competition.service_loops import run_intake_cycle
    rows=[{'round_id':'a','network':'testnet','status':'final','deadline_at':'2026-11-02T12:00:00+00:00'},
          {'round_id':'b','network':'testnet','status':'open','deadline_at':'2026-11-03T12:00:00+00:00','deadline_block':100},
          {'round_id':'c','network':'testnet','status':'open','deadline_at':'2026-11-04T12:00:00+00:00','deadline_block':200}]
    backend=SimpleNamespace(list_pending_rounds=lambda _:rows,
        list_submissions=lambda _:[{'qualified':True,'status':'accepted'}])
    seen=[];closed=[]
    def inputs(row):
        seen.append(row['round_id'])
        return SimpleNamespace(archive=SimpleNamespace(get_current_block=lambda:100),netuid=530,
            package_for_submission=lambda _:b'package',close=lambda:closed.append(row['round_id']))
    monkeypatch.setattr('competition.round_controller.lock_ready_round',lambda *a,**kw:{'status':'locked'})
    assert run_intake_cycle(backend=backend,network='testnet',inputs_for_round=inputs,
        now=datetime(2026,11,3,12,tzinfo=UTC))==[{'round_id':'b','status':'locked'}]
    assert seen==closed==['b']


class Backend:
    def list_pending_rounds(self, network):
        assert network == "testnet"
        return [
            {"round_id": "old", "network": network, "status": "screening",
             "configuration_sha256": "a" * 64},
            {"round_id": "new", "network": network, "status": "final",
             "configuration_sha256": "b" * 64},
        ]


def test_stopped_rounds_never_construct_or_run_adapters():
    from competition.service_loops import run_bronze_cycle
    backend = SimpleNamespace(list_pending_rounds=lambda network: [
        {"round_id": "stopped", "network": network, "status": "failed"},
    ])

    def forbidden(*args):
        raise AssertionError("stopped round resumed")

    assert run_controller_cycle(backend=backend, network="testnet", inputs_for_round=forbidden) == []
    assert run_bronze_cycle(backend=backend, network="testnet", inputs_for_round=forbidden) == []
    assert run_worker_cycle(backend=backend, network="testnet", coordinator_for_round=forbidden) == []


def test_controller_cycle_resumes_all_rounds_after_one_fault(monkeypatch):
    backend = Backend()
    seen = []
    closed = []

    def inputs(row):
        return SimpleNamespace(
            evaluation=SimpleNamespace(round_id=row["round_id"],
                                       configuration_sha256=row["configuration_sha256"]),
            paper_inputs=SimpleNamespace(backend=backend),
            close=lambda: closed.append(row["round_id"]),
        )

    def tick(round_id, **kwargs):
        seen.append(round_id)
        if round_id == "old":
            raise RuntimeError("private provider response")
        return {"status": "waiting_for_final_scores", "gold_check_seed_private": "secret"}

    monkeypatch.setattr("competition.service_loops.tick_round", tick)
    result = run_controller_cycle(
        backend=backend, network="testnet", inputs_for_round=inputs,
        now=datetime(2026, 11, 9, 12, tzinfo=UTC),
    )
    assert seen == ["old", "new"]
    assert closed == ["old", "new"]
    assert result == [
        {"round_id": "old", "status": "error", "error_type": "RuntimeError"},
        {"round_id": "new", "status": "waiting_for_final_scores"},
    ]
    assert "private provider response" not in str(result)
    assert "secret" not in str(result)


def test_worker_cycle_only_claims_active_matching_rounds():
    backend = Backend()
    seen = []

    def coordinator(row):
        class Worker:
            round_id = row["round_id"]
            network = "testnet"
            backend = None

            def run_once(self):
                seen.append(self.round_id)
                return {"status": "completed", "artifact": {"private": "result"}}

        worker = Worker()
        worker.backend = backend
        return worker

    results = run_worker_cycle(
        backend=backend, network="testnet", coordinator_for_round=coordinator,
    )
    assert seen == ["old", "new"]
    assert results == [
        {"round_id": "old", "status": "job_processed", "worker_status": "completed"},
        {"round_id": "new", "status": "job_processed", "worker_status": "completed"},
    ]


def test_bronze_service_only_prepares_rounds_with_frozen_pipeline_policy():
    from competition.service_loops import run_bronze_cycle
    backend = Backend()
    backend.get_round = lambda rid: {"rules_public": {"policy": ({"bronze": {"mode": "after_reveal"},
        "execution": {"schema_version": "claims_competition_execution_v1", "bronze_max_workers": 3,
        "scoring_max_workers": 2, "decision_max_workers": 8}} if rid == "new" else {})}}
    called = []
    def inputs(row):
        return SimpleNamespace(prepare_bronze=lambda rid: called.append(rid) or {"status": "bronze_ready"}, close=lambda: None)
    assert run_bronze_cycle(backend=backend, network="testnet", inputs_for_round=inputs) == [
        {"round_id": "new", "status": "bronze_ready"}]
    assert called == ["new"]
