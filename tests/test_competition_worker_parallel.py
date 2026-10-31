"""Memory admission, package affinity and rolling execution regression tests."""

import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

from competition.worker_capacity import (
    GIB,
    SCHEMA,
    memory_slots,
    validate_capacity_config,
)
from competition.worker_control import execution_slot, load_config
from competition.worker_service import CompetitionWorkerCoordinator


CONFIG = {
    "schema_version": SCHEMA,
    "memory_per_job_bytes": 4 * GIB,
    "host_reserved_memory_bytes": 8 * GIB,
    "max_parallel_jobs": 64,
}


def test_memory_budget_reserves_full_limits_and_host_headroom():
    assert memory_slots(CONFIG, 50 * GIB, 49 * GIB) == (10, 10)
    assert memory_slots(CONFIG, 50 * GIB, 15 * GIB) == (10, 1)
    assert memory_slots(CONFIG, 50 * GIB, 9 * GIB) == (10, 0)
    assert memory_slots(CONFIG, 10 * GIB, 9 * GIB) == (0, 0)
    assert memory_slots({**CONFIG, "max_parallel_jobs": 3}, 50 * GIB, 49 * GIB) == (
        3,
        3,
    )
    with pytest.raises(ValueError):
        validate_capacity_config({**CONFIG, "memory_per_job_bytes": True})


def test_root_slots_enforce_capacity_and_single_package_then_reuse(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "competition.worker_control.memory_info", lambda: (16 * GIB, 16 * GIB)
    )
    args = {"lock_dir": tmp_path / "slots", "require_root_owned": False}
    with execution_slot(CONFIG, "a" * 64, **args) as first:
        with pytest.raises(RuntimeError, match="another package"):
            with execution_slot(CONFIG, "b" * 64, **args):
                pass
        with execution_slot(CONFIG, "a" * 64, **args) as second:
            assert {first, second} == {0, 1}
            with pytest.raises(RuntimeError, match="no memory slot"):
                with execution_slot(CONFIG, "a" * 64, **args):
                    pass
        with execution_slot(CONFIG, "a" * 64, **args) as reused:
            assert reused == second
    with execution_slot(CONFIG, "b" * 64, **args) as fresh:
        assert fresh == 0


def test_memory_pressure_blocks_admission_without_breaking_active_slot(
    tmp_path, monkeypatch
):
    memory = [(16 * GIB, 16 * GIB)]
    monkeypatch.setattr("competition.worker_control.memory_info", lambda: memory[0])
    with execution_slot(
        CONFIG, "a" * 64, lock_dir=tmp_path / "slots", require_root_owned=False
    ):
        memory[0] = (16 * GIB, 9 * GIB)
        with pytest.raises(RuntimeError, match="no memory slot"):
            with execution_slot(
                CONFIG, "a" * 64, lock_dir=tmp_path / "slots", require_root_owned=False
            ):
                pass


def test_parallel_coordinator_refills_before_slowest_sibling_finishes(monkeypatch):
    monkeypatch.setattr(
        CompetitionWorkerCoordinator,
        "_validated_policy",
        lambda self: self.evaluation.configuration["worker_policy"],
    )
    lock = threading.Lock()
    release = threading.Event()
    state = {"active": 0, "peak": 0, "completed": [], "filters": []}
    jobs = deque(
        {
            "job_id": str(i),
            "hotkey": "miner",
            "package_sha256": "a" * 64,
            "stage": "prescreen",
        }
        for i in range(12)
    )

    class Backend:
        def claim_job(self, rid, **kwargs):
            state["filters"].append(kwargs)
            return jobs.popleft() if jobs else None

    backend = Backend()
    packages = SimpleNamespace(backend=backend)
    c = CompetitionWorkerCoordinator(
        "r",
        "testnet",
        "worker",
        backend,
        SimpleNamespace(round_id="r", backend=backend),
        packages,
        SimpleNamespace(
            round_id="r", configuration={"worker_policy": {"lease_seconds": 60}}
        ),
        None,
        None,
        SimpleNamespace(capacity=lambda: {"slots": 3, "available_slots": 3}),
        None,
    )

    def run(self, *, claimed_job=None):
        job = claimed_job["job_id"]
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            if job == "0":
                assert release.wait(3), (
                    "rolling refill did not start while first paper was running"
                )
            elif int(job) >= 3:
                release.set()
            else:
                time.sleep(0.03)
            with lock:
                state["completed"].append(job)
            return {"status": "completed", "job_id": job}
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(CompetitionWorkerCoordinator, "run_once", run)
    results = c.run_available()
    assert len(results) == 12 and state["peak"] == 3 and not jobs
    assert len(set(state["completed"])) == 12
    assert all(
        row["hotkey"] == "miner" and row["stage"] == "prescreen"
        for row in state["filters"][1:]
    )


def test_batch_package_fetch_is_shared_but_manifest_copies_are_private():
    from competition.worker_service import _BatchPackages

    counts = {"manifest": 0, "package": 0}

    class Source:
        backend = object()

        def manifest_for_job(self, job):
            counts["manifest"] += 1
            return {"models": ["allowed"]}

        def __call__(self, job):
            counts["package"] += 1
            return b"sealed-package"

    cached = _BatchPackages(Source())
    job = {
        "round_id": "r",
        "stage": "final",
        "hotkey": "miner",
        "package_sha256": "a" * 64,
    }
    first = cached.manifest_for_job(job)
    first["models"].append("mutated")
    assert cached.manifest_for_job(job) == {"models": ["allowed"]}
    assert cached(job) == cached(job) == b"sealed-package"
    assert counts == {"manifest": 1, "package": 1}
    with pytest.raises(ValueError, match="another package"):
        cached({**job, "hotkey": "other"})


def test_evaluation_polling_does_not_open_chain_until_needed():
    from competition.chain_archive import LazySubtensor

    calls = []
    client = SimpleNamespace(
        close=lambda: calls.append("close"), get_current_block=lambda: 123
    )
    chain = LazySubtensor(lambda: calls.append("open") or client)
    chain.close()
    assert calls == []
    assert chain.get_current_block() == 123
    assert chain.get_current_block() == 123 and calls == ["open"]
    chain.close()
    assert calls == ["open", "close"]


def test_unclean_slot_is_quarantined_and_does_not_reuse_its_account(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "competition.worker_control.memory_info", lambda: (16 * GIB, 16 * GIB)
    )
    jobs = tmp_path / "jobs"
    (jobs / "slot-0" / "unfinished").mkdir(parents=True)
    monkeypatch.setattr("competition.worker_control.JOBS_DIR", jobs)
    with execution_slot(
        CONFIG, "a" * 64, lock_dir=tmp_path / "slots", require_root_owned=False
    ) as slot:
        assert slot == 1


def test_v2_control_config_validates_memory_and_gateway(tmp_path):
    import json

    config = {
        **CONFIG,
        "inference_base_url": "https://gateway.example/v1",
        "gateway_ip": "192.0.2.2",
        "gateway_port": 443,
    }
    path = tmp_path / "control.json"
    path.write_text(json.dumps(config))
    assert load_config(path, require_root_owned=False) == config
    path.write_text(json.dumps({**config, "memory_per_job_bytes": True}))
    with pytest.raises(ValueError, match="memory admission"):
        load_config(path, require_root_owned=False)


def test_worker_rejects_host_memory_limit_different_from_frozen_round(monkeypatch):
    monkeypatch.setattr(
        CompetitionWorkerCoordinator,
        "_validated_policy",
        lambda self: self.evaluation.configuration["worker_policy"],
    )
    backend = SimpleNamespace(
        claim_job=lambda *_, **__: pytest.fail("must reject before leasing")
    )
    coordinator = CompetitionWorkerCoordinator(
        "r",
        "testnet",
        "w",
        backend,
        SimpleNamespace(round_id="r", backend=backend),
        SimpleNamespace(backend=backend),
        SimpleNamespace(
            round_id="r",
            configuration={
                "worker_policy": {"memory_per_job_bytes": 4 * GIB, "lease_seconds": 60}
            },
        ),
        None,
        None,
        SimpleNamespace(
            capacity=lambda: {
                "slots": 10,
                "available_slots": 10,
                "memory_per_job_bytes": 2 * GIB,
            }
        ),
        None,
    )
    with pytest.raises(ValueError, match="memory limit differs"):
        coordinator.run_available()


def test_staged_input_bytes_count_against_host_reservations(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "competition.worker_control.memory_info", lambda: (16 * GIB, 16 * GIB)
    )
    args = {"lock_dir": tmp_path / "slots", "require_root_owned": False}
    with execution_slot(CONFIG, "a" * 64, staging_bytes=100_000_000, **args):
        with pytest.raises(RuntimeError, match="no memory slot"):
            with execution_slot(CONFIG, "a" * 64, staging_bytes=100_000_000, **args):
                pass


def test_changed_runtime_is_rejected_before_capacity_probe_or_leasing():
    from competition.publication import canonical_sha256

    config = {
        "competition_policy_runtime": {"old": "runtime"},
        "worker_policy": {"lease_seconds": 60},
    }
    digest = canonical_sha256(config)
    backend = SimpleNamespace(
        get_round=lambda _: {
            "round_id": "r",
            "network": "testnet",
            "configuration_sha256": digest,
        },
        claim_job=lambda *_, **__: pytest.fail(
            "must not lease from a frozen historical runtime"
        ),
    )
    coordinator = CompetitionWorkerCoordinator(
        "r",
        "testnet",
        "w",
        backend,
        SimpleNamespace(round_id="r", backend=backend),
        SimpleNamespace(backend=backend),
        SimpleNamespace(
            round_id="r", configuration_sha256=digest, configuration=config
        ),
        None,
        None,
        SimpleNamespace(
            capacity=lambda: pytest.fail("must validate before touching execution VM")
        ),
        None,
    )
    with pytest.raises(ValueError, match="frozen round"):
        coordinator.run_available()
