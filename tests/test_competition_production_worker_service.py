from datetime import UTC, datetime
from hashlib import sha256

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
