from datetime import UTC, datetime
from types import SimpleNamespace

from competition.service_loops import run_controller_cycle, run_worker_cycle


class Backend:
    def list_pending_rounds(self, network):
        assert network == "testnet"
        return [
            {"round_id": "old", "network": network, "status": "screening",
             "configuration_sha256": "a" * 64},
            {"round_id": "new", "network": network, "status": "final",
             "configuration_sha256": "b" * 64},
        ]


def test_controller_cycle_resumes_all_rounds_after_one_fault(monkeypatch):
    backend = Backend()
    seen = []

    def inputs(row):
        return SimpleNamespace(
            evaluation=SimpleNamespace(round_id=row["round_id"],
                                       configuration_sha256=row["configuration_sha256"]),
            paper_inputs=SimpleNamespace(backend=backend),
        )

    def tick(round_id, **kwargs):
        seen.append(round_id)
        if round_id == "old":
            raise RuntimeError("private provider response")
        return {"status": "waiting_for_final_scores"}

    monkeypatch.setattr("competition.service_loops.tick_round", tick)
    result = run_controller_cycle(
        backend=backend, network="testnet", inputs_for_round=inputs,
        now=datetime(2026, 11, 9, 12, tzinfo=UTC),
    )
    assert seen == ["old", "new"]
    assert result == [
        {"round_id": "old", "status": "error", "error_type": "RuntimeError"},
        {"round_id": "new", "status": "waiting_for_final_scores"},
    ]
    assert "private provider response" not in str(result)


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
