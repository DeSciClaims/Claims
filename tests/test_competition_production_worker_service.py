from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from competition.production_reader import ActiveProductionPackage
from competition.production_worker_service import ProductionWorkerCoordinator


def _active():
    at = datetime(2026, 11, 2, 9, tzinfo=UTC)
    package = b"package"
    return ActiveProductionPackage(
        "week2", "a" * 64, "miner", sha256(package).hexdigest(),
        at, at, None, package, {"entrypoint": "sample.cli:main"},
    )


def test_production_worker_claims_only_active_receipt(monkeypatch):
    active = _active()
    seen = []

    class Backend:
        def pending_production_papers(self, **kwargs):
            assert kwargs["receipt_sha256"] == active.receipt_sha256
            return [{"paper_id": "paper-1", "title": "Paper", "doi": None,
                     "source_url": "https://papers.example/paper.pdf",
                     "source_sha256": "c" * 64}]

        def enqueue_production_job(self, job):
            assert job["package_sha256"] == active.package_sha256
            assert job["paper"] == {"paper_id": "paper-1", "title": "Paper"}
            return job

        def claim_production_job(self, **kwargs):
            seen.append(kwargs)
            return {"job_id": "b" * 64}

    class Reader:
        def read_active(self):
            return active

    class Fetcher:
        def fetch(self, url, digest):
            return b"PDF"

    def run(release, lease, **kwargs):
        assert release == active
        assert lease["job_id"] == "b" * 64
        assert kwargs["worker_id"] == "production-worker"
        assert kwargs["token_limit"] == 1000
        return {"status": "completed"}

    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    worker = ProductionWorkerCoordinator(
        network="testnet", worker_id="production-worker",
        production_start_at="2026-11-02T09:00:00+00:00",
        backend=Backend(), reader=Reader(), rules_for_release=lambda _: {
            "lease_seconds": 60, "inference_base_url": "https://gateway.example/v1",
            "allowed_models": ["example/model"],
            "published_token_ceiling": 1000, "dollar_limit_usd": 1,
            "max_inference_output_tokens": 8192,
        }, pdf_fetcher=Fetcher(), keys=object(), execution=object(), logs=object(),
    )
    assert worker.run_once() == {"status": "completed"}
    assert seen == [{"network": "testnet", "receipt_sha256": "a" * 64,
                    "worker_id": "production-worker", "lease_seconds": 60}]


def test_production_worker_waits_for_active_release():
    class Reader:
        def read_active(self):
            return None

    worker = ProductionWorkerCoordinator(
        network="testnet", worker_id="production-worker",
        production_start_at="2026-11-02T09:00:00+00:00", backend=object(),
        reader=Reader(), rules_for_release=lambda _: {}, pdf_fetcher=object(),
        keys=object(), execution=object(), logs=object(),
    )
    assert worker.run_once() is None


# Real threads and deterministic events exercise rolling admission, rather than
# merely checking the constructor's configured concurrency value.
POLICY = {
    "lease_seconds": 60, "inference_base_url": "https://gateway.example/v1",
    "allowed_models": ["example/model"], "published_token_ceiling": 1000,
    "dollar_limit_usd": 1, "max_inference_output_tokens": 8192,
    "memory_per_job_bytes": 4 * 1024 ** 3,
}


class BatchBackend:
    def __init__(self, size, *, busy=False):
        self.jobs = [{"job_id": str(i), "paper_id": f"paper-{i}"} for i in range(size)]
        self.claims = []
        self.completed = []
        self.busy = busy
        self.lock = Lock()
        self.activity_checks = 0

    def pending_production_papers(self, **kwargs):
        return []

    def enqueue_production_job(self, _):
        raise AssertionError("fixture jobs already queued")

    def list_pending_rounds(self, network):
        self.activity_checks += 1
        return [{"network": network, "status": "final" if self.busy else "open"}]

    def claim_production_job(self, **kwargs):
        with self.lock:
            if not self.jobs:
                return None
            job = self.jobs.pop(0)
            self.claims.append((job["job_id"], kwargs["receipt_sha256"]))
            return job


def batch_worker(size=5, *, slots=4, busy=False):
    backend = BatchBackend(size, busy=busy)
    reader = SimpleNamespace(active=_active())
    reader.read_active = lambda: reader.active
    execution = SimpleNamespace(capacity=lambda: {
        "schema_version": "claims_worker_capacity_v1", "slots": slots,
        "available_slots": slots, "memory_per_job_bytes": POLICY["memory_per_job_bytes"],
    })
    return ProductionWorkerCoordinator(
        network="testnet", worker_id="production-worker",
        production_start_at="2026-08-01T00:00:00+00:00",
        backend=backend, reader=reader, rules_for_release=lambda _: POLICY,
        pdf_fetcher=SimpleNamespace(fetch=lambda *a: b"PDF"),
        keys=object(), execution=execution, logs=object(),
        max_parallel_jobs=3, competition_parallel_jobs=1,
    )


def in_thread(fn):
    outcome = {}
    def target():
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["error"] = exc
    thread = Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def finish(thread, outcome):
    thread.join(5)
    assert not thread.is_alive(), "production batch failed to drain"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def test_parallel_batch_refills_without_waiting_for_slow_sibling(monkeypatch):
    worker = batch_worker(size=5)
    slow_release = Event()
    initial = Event()
    refill = Event()
    started = []
    lock = Lock()
    def run(active, job, **kwargs):
        assert kwargs["token_limit"] == 1000 and kwargs["dollar_limit"] == 1
        with lock:
            started.append(job["job_id"])
            if len(started) == 3:
                initial.set()
        assert initial.wait(3)
        if job["job_id"] == "0":
            assert slow_release.wait(3)
        if job["job_id"] == "3":
            refill.set()
        worker.backend.completed.append(job["job_id"])
        return {"status": "completed", "artifact": {"private_claim": "not retained"}}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    events = []
    thread, result = in_thread(lambda: worker.run_available(on_completed=events.append))
    try:
        assert refill.wait(3), "freed slots should refill while the first paper is still running"
        assert "0" not in worker.backend.completed
    finally:
        slow_release.set()
    rows = finish(thread, result)
    assert len(rows) == len(events) == 5
    assert len(set(i for i, _ in worker.backend.claims)) == 5
    assert all("artifact" not in row and "private_claim" not in str(row) for row in rows)
    # A reconstructed coordinator sees only backend jobs not already completed.
    assert replace(worker).run_available() == []
    assert len(worker.backend.claims) == 5


def test_competition_activity_reduces_parallel_admission(monkeypatch):
    worker = batch_worker(size=4, busy=True)
    first_started, release_first = Event(), Event()
    def run(active, job, **kwargs):
        if job["job_id"] == "0":
            first_started.set()
            assert release_first.wait(3)
        return {"status": "completed"}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    thread, result = in_thread(worker.run_available)
    try:
        assert first_started.wait(3)
        assert len(worker.backend.claims) == 1
    finally:
        release_first.set()
    assert len(finish(thread, result)) == 4


def test_vm_capacity_overrides_administrative_parallel_limit(monkeypatch):
    worker = batch_worker(size=2, slots=1)
    first_started, release_first = Event(), Event()
    def run(active, job, **kwargs):
        if job["job_id"] == "0":
            first_started.set()
            assert release_first.wait(3)
        return {"status": "completed"}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    thread, result = in_thread(worker.run_available)
    try:
        assert first_started.wait(3)
        assert len(worker.backend.claims) == 1
    finally:
        release_first.set()
    assert len(finish(thread, result)) == 2


def test_new_winner_stops_admissions_and_keeps_inflight_results(monkeypatch):
    worker = batch_worker(size=5)
    first_started, release_first = Event(), Event()
    old = worker.reader.active
    worker = replace(worker, max_parallel_jobs=1)
    def run(active, job, **kwargs):
        assert active == old
        assert "is_current_release" not in kwargs
        first_started.set()
        assert release_first.wait(3)
        worker.backend.completed.append(job["job_id"])
        return {"status": "completed"}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    thread, result = in_thread(worker.run_available)
    try:
        assert first_started.wait(3)
        worker.reader.active = replace(old, receipt_sha256="d" * 64)
    finally:
        release_first.set()
    assert len(finish(thread, result)) == 1
    assert worker.backend.completed == ["0"]
    assert worker.backend.claims == [("0", old.receipt_sha256)]


def test_one_job_exception_preserves_all_admitted_sibling_results(monkeypatch):
    worker = batch_worker(size=5)
    initial = Event()
    lock = Lock()
    started = []
    def run(active, job, **kwargs):
        with lock:
            started.append(job["job_id"])
            if len(started) == 3:
                initial.set()
        assert initial.wait(3)
        if job["job_id"] == "0":
            raise RuntimeError("private failure details")
        worker.backend.completed.append(job["job_id"])
        return {"status": "completed"}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    with pytest.raises(RuntimeError, match="batch paused") as error:
        worker.run_available()
    assert "private failure details" not in str(error.value)
    # A fast sibling may free a slot before the error future becomes visible;
    # every already admitted sibling must still complete durably.
    assert set(worker.backend.completed) == {j for j, _ in worker.backend.claims} - {"0"}
    assert len(set(j for j, _ in worker.backend.claims)) == len(worker.backend.claims)


def test_activity_failure_does_not_lease_any_jobs(monkeypatch):
    worker = batch_worker()
    worker.backend.list_pending_rounds = lambda _: [{"network": "wrong", "status": "final"}]
    with pytest.raises(ValueError, match="activity response"):
        worker.run_available()
    assert worker.backend.claims == []


@pytest.mark.parametrize("capacity", [
    {"schema_version": "claims_worker_capacity_v1", "slots": 10, "available_slots": 11},
    {"schema_version": "claims_worker_capacity_v1", "slots": True, "available_slots": 1},
    {"schema_version": "wrong", "slots": 1, "available_slots": 1},
    {"schema_version": "claims_worker_capacity_v1", "slots": 1, "available_slots": 1,
     "memory_per_job_bytes": 1},
])
def test_invalid_capacity_does_not_lease_any_jobs(capacity):
    worker = batch_worker()
    worker.execution.capacity = lambda: capacity
    with pytest.raises(ValueError, match="memory capacity"):
        worker.run_available()
    assert worker.backend.claims == []


def test_no_capacity_does_not_even_enqueue_or_query_competition():
    worker = batch_worker(slots=0)
    assert worker.run_available() == []
    assert not worker.backend.claims and worker.backend.activity_checks == 0


def test_batch_size_bounds_each_cycle_and_does_not_repeat_completed_jobs(monkeypatch):
    worker = replace(batch_worker(size=5), producer_batch_size=2)
    monkeypatch.setattr("competition.production_worker_service.run_production_paper",
                        lambda *a, **kw: {"status": "completed"})
    assert len(worker.run_available()) == 2
    assert len(worker.run_available()) == 2
    assert len(worker.run_available()) == 1
    assert [j for j, _ in worker.backend.claims] == ["0", "1", "2", "3", "4"]


def test_observed_job_error_stops_further_admissions(monkeypatch):
    worker = replace(batch_worker(size=4), max_parallel_jobs=1)
    def broken(*args, **kwargs):
        raise ValueError("private error")
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", broken)
    with pytest.raises(RuntimeError, match="batch paused"):
        worker.run_available()
    assert [j for j, _ in worker.backend.claims] == ["0"]
    assert len(worker.backend.jobs) == 3


def test_new_competition_activity_drains_to_lower_limit_without_killing_jobs(monkeypatch):
    import itertools
    worker = replace(batch_worker(size=4), max_parallel_jobs=2, competition_check_seconds=1)
    clock = itertools.count(step=2)
    monkeypatch.setattr("competition.production_worker_service.monotonic", lambda: next(clock))
    two_started, throttled, release_slow = Event(), Event(), Event()
    started = []
    lock = Lock()
    original_activity = worker.backend.list_pending_rounds
    def activity(network):
        if worker.backend.busy:
            throttled.set()
        return original_activity(network)
    worker.backend.list_pending_rounds = activity
    def run(active, job, **kwargs):
        with lock:
            started.append(job["job_id"])
            if len(started) == 2:
                two_started.set()
        assert two_started.wait(3)
        if job["job_id"] == "0":
            worker.backend.busy = True
        elif job["job_id"] == "1":
            assert release_slow.wait(3)
        return {"status": "completed"}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    thread, result = in_thread(worker.run_available)
    try:
        assert throttled.wait(3)
        assert [j for j, _ in worker.backend.claims] == ["0", "1"]
    finally:
        release_slow.set()
    assert len(finish(thread, result)) == 4


def test_readiness_is_read_only_and_reports_effective_competition_limit():
    worker = batch_worker(busy=True)
    calls = []
    def pending(**kwargs):
        calls.append(kwargs)
        return [{"paper_id": "candidate"}]
    worker.backend.pending_production_papers = pending
    ready = worker.check_ready()
    assert ready["status"] == "production_ready"
    assert ready["current_parallel_limit"] == 1
    assert ready["backlog_available"] is True
    assert ready["package_sha256"] == worker.reader.active.package_sha256
    assert calls[0]["limit"] == 1
    assert worker.backend.claims == [] and len(worker.backend.jobs) == 5


@pytest.mark.parametrize("settings", [
    {"max_parallel_jobs": 0}, {"max_parallel_jobs": True},
    {"competition_parallel_jobs": 4}, {"producer_batch_size": 0},
    {"competition_check_seconds": float("nan")}, {"production_start_at": "yesterday"},
])
def test_invalid_scheduling_configuration_fails_before_leasing(settings):
    worker = replace(batch_worker(), **settings)
    with pytest.raises(ValueError):
        worker.run_available()
    assert not worker.backend.claims


def test_factory_reads_production_scheduling_from_environment(monkeypatch, tmp_path):
    from competition import pilot_production_worker_factory as factory
    values = {
        "CLAIMS_COMPETITION_PRODUCTION_ROOT": str(tmp_path),
        "CLAIMS_PRODUCTION_START_AT": "2026-08-24T00:00:00Z",
        "CLAIMS_PRODUCTION_INFERENCE_BASE_URL": "https://gateway.example:8443/v1",
        "CLAIMS_HIPPIUS_ENDPOINT": "https://s3.example",
        "CLAIMS_HIPPIUS_ACCESS_KEY_ID": "test", "CLAIMS_HIPPIUS_SECRET_ACCESS_KEY": "test",
        "CLAIMS_HIPPIUS_PACKAGE_BUCKET": "packages", "CLAIMS_HIPPIUS_LOG_BUCKET": "logs",
        "CLAIMS_WORKER_SSH_IDENTITY": str(tmp_path / "key"),
        "CLAIMS_WORKER_SSH_KNOWN_HOSTS": str(tmp_path / "hosts"),
        "CLAIMS_WORKER_GATEWAY_IP": "100.98.33.109",
        "CLAIMS_PRODUCTION_MAX_PARALLEL_JOBS": "8",
        "CLAIMS_PRODUCTION_COMPETITION_PARALLEL_JOBS": "2",
        "CLAIMS_PRODUCTION_BATCH_SIZE": "20",
        "CLAIMS_PRODUCTION_ACTIVITY_POLL_SECONDS": "15",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(factory, "hippius_s3_client", lambda **_: object())
    monkeypatch.setattr(factory, "_bifrost_admin_client", lambda: object())
    monkeypatch.setattr(factory, "RestrictedSshWorkerTransport", lambda **_: object())
    monkeypatch.setattr(factory, "_worker_execution", lambda _: object())
    worker = factory.create_production_worker(object(), network="mainnet", worker_id="prod-1")
    worker._validate()
    assert (worker.max_parallel_jobs, worker.competition_parallel_jobs,
            worker.producer_batch_size, worker.competition_check_seconds) == (8, 2, 20, 15)


def test_small_vm_does_not_lease_jobs_waiting_inside_the_thread_pool(monkeypatch):
    worker = replace(batch_worker(size=6, slots=2), max_parallel_jobs=10)
    original_claim = worker.backend.claim_production_job
    def claim(**kwargs):
        assert len(worker.backend.claims) - len(worker.backend.completed) < 2
        return original_claim(**kwargs)
    worker.backend.claim_production_job = claim
    def run(active, job, **kwargs):
        worker.backend.completed.append(job["job_id"])
        return {"status": "completed"}
    monkeypatch.setattr("competition.production_worker_service.run_production_paper", run)
    assert len(worker.run_available()) == 6
