import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from bittensor_wallet import Keypair

from competition.bifrost import PaperKey, PaperUsage
from competition.paper_score_controller import _status_fault
from competition.worker_controller import (
    ExecutionOutput,
    paper_token_cap,
    run_claimed_job,
    run_next_claimed_job,
)


def artifact():
    return {
        "paper": {"paper_id": "paper-1", "title": "Paper"},
        "logic": {
            "claims": [{"claim_id": "C1", "statement": "Result", "conditions": "Study",
                        "status": "supported", "falsification_criteria": "No result",
                        "proof": ["E1"], "evidence_ids": ["EV1"]}],
            "experiments": [{"experiment_id": "E1", "title": "Test", "verifies": ["C1"],
                             "setup": "Groups", "procedure": "Compare", "expected_outcome": "Result",
                             "evidence_ids": ["EV1"]}],
        },
        "evidence": {"records": [{"evidence_id": "EV1", "title": "Result", "role": "support",
                                 "summary": "Result", "linked_claim_ids": ["C1"]}]},
        "trace": {"node_id": "root", "node_type": "extraction", "summary": "Result"},
        "src": {"environment": ["smoke"], "artifacts": []},
    }


class Keys:
    def __init__(self, over=False, expected_cap=1000):
        self.over = over
        self.expected_cap = expected_cap
        self.revoked = False
        self.created = False

    def create_paper_key(self, **kwargs):
        self.created = True
        assert kwargs["token_limit"] == self.expected_cap
        assert kwargs["allowed_models"] == ("example/model",)
        return PaperKey("key-1", "sk-bf-short-lived", datetime.now(UTC) + timedelta(minutes=10),
                        self.expected_cap, kwargs["dollar_limit"])

    def check_budget(self, key):
        assert key.value == "sk-bf-short-lived"
        return PaperUsage(1200 if self.over else 350, 0.12, self.over)

    def revoke(self, key):
        assert key.value == "sk-bf-short-lived"
        self.revoked = True


class Executor:
    def run(self, **kwargs):
        assert kwargs["paper_key"] == "sk-bf-short-lived"
        assert kwargs["job"]["worker_contract"]["request"]["pdf_path"] == "paper.pdf"
        assert kwargs["job"]["worker_contract"]["entrypoint"] == "claims_sample_miner.cli:main"
        return ExecutionOutput("completed", artifact(), b"installed\nran sk-bf-short-lived\n")


class Logs:
    def __init__(self):
        self.key = None
        self.content = None

    def put(self, key, content):
        self.key, self.content = key, content


class Backend:
    def __init__(self, job=None, *, challenger=False, baseline_status="completed", baseline_usage=350):
        self.receipt = None
        self.job = job
        self.challenger = challenger
        self.baseline_status = baseline_status
        self.baseline_usage = baseline_usage

    def list_stages(self, _round_id):
        baseline_hotkey = "5" * 48 if self.challenger else self.job["hotkey"]
        baseline_digest = "b" * 64 if self.challenger else self.job["package_sha256"]
        entries = [{"hotkey": baseline_hotkey, "package_sha256": baseline_digest,
                    "role": "reference"}]
        if self.challenger:
            entries.append({"hotkey": self.job["hotkey"],
                            "package_sha256": self.job["package_sha256"], "role": "challenger"})
        return [{"stage": "week_one", "entries": entries}]

    def list_paper_jobs(self, *_):
        return [{"hotkey": "5" * 48, "package_sha256": "b" * 64,
                 "status": "completed", "result_private": {
                     "status": self.baseline_status,
                     "prompt_and_completion_tokens": self.baseline_usage,
                 }}]

    def complete_job(self, job_id, **kwargs):
        self.receipt = (job_id, kwargs)
        return {"status": "completed"}


def run(over=False, package=b"package", pdf=b"%PDF sample", challenger=False, execution=None,
        attempt=1, hotkey=None):
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    job = {
        "job_id": "a" * 64, "round_id": "week2", "hotkey": hotkey or signer.ss58_address,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "stage": "week_one", "paper_id": "paper-1", "lease_token": "lease-token",
        "attempts": attempt,
        "paper": {"paper_id": "paper-1", "title": "Paper"},
        "manifest": {
            "schema_version": "claims_competition_manifest_v1", "language": "python",
            "package_format": "wheel",
            "package_filename": "claims_sample_miner-0.1.0-py3-none-any.whl", "source_included": True,
            "license_production_use": True, "entrypoint": "claims_sample_miner.cli:main",
            "models": ["example/model"],
        },
    }
    keys, logs, backend = Keys(over, expected_cap=525 if challenger else 1000), Logs(), Backend(job, challenger=challenger)
    result = run_claimed_job(
        job, backend=backend, worker_id="worker-1", network="testnet",
        fetch_package=lambda _job: package, fetch_pdf=lambda _job: pdf,
        expected_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        keys=keys, execution=execution or Executor(), logs=logs,
        inference_base_url="http://gateway.internal:8080/v1",
        allowed_models=("example/model",), published_token_ceiling=1000, dollar_limit=1.0,
    )
    return result, keys, logs, backend, job


def test_worker_controller_revokes_key_before_private_log_and_completion():
    result, keys, logs, backend, job = run()
    assert result["status"] == "completed"
    assert result["prompt_and_completion_tokens"] == 350
    assert result["inference_cost_usd"] == 0.12
    assert keys.revoked
    assert logs.key.startswith(f"logs/testnet/week2/{job['hotkey']}/")
    assert logs.key.endswith("-attempt-1.txt")
    assert b"sk-bf-short-lived" not in logs.content
    assert b"[REDACTED_PAPER_KEY]" in logs.content
    assert backend.receipt[0] == job["job_id"]
    assert backend.receipt[1]["result"]["artifact"]["paper"]["paper_id"] == "paper-1"


def test_worker_controller_persists_trusted_vm_estimate():
    class TimedExecutor(Executor):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            return ExecutionOutput(result.status, result.artifact, result.log_bytes,
                                   vm_elapsed_seconds=36, vm_cost_estimate_usd=0.01)

    result, _, _, backend, _ = run(execution=TimedExecutor())
    assert result["vm_elapsed_seconds"] == 36
    assert result["vm_cost_estimate_usd"] == 0.01
    assert backend.receipt[1]["result"]["vm_cost_estimate_usd"] == 0.01


def test_retry_writes_a_distinct_log_object():
    _, _, first_logs, _, _ = run(attempt=1)
    _, _, second_logs, _, _ = run(attempt=2)
    assert first_logs.key != second_logs.key
    assert second_logs.key.endswith("-attempt-2.txt")


def test_reference_service_identity_can_run_and_store_a_log():
    result, _, logs, _, job = run(hotkey="claims-reference")
    assert result["status"] == "completed"
    assert f"/{job['hotkey']}/" in logs.key


def test_over_cap_output_is_not_scored_as_a_success():
    result, keys, _logs, backend, _job = run(over=True)
    assert keys.revoked
    assert result["status"] == "cap_exceeded"
    assert backend.receipt[1]["result"]["artifact"] is None


def test_denied_network_attempt_is_a_package_fault_with_no_artifact():
    class Denied:
        def run(self, **_):
            return ExecutionOutput("security_violation", None, b"blocked egress",
                                   denied_egress_packets=2)

    result, keys, logs, backend, _job = run(execution=Denied())
    assert result["status"] == "security_violation"
    assert result["artifact"] is None
    assert result["denied_egress_packets"] == 2
    assert keys.revoked
    assert logs.content == b"blocked egress"
    assert backend.receipt[1]["result"]["status"] == "security_violation"
    assert _status_fault(result["status"]) == "package"


def test_challenger_receives_same_paper_baseline_cap():
    result, keys, _logs, backend, _job = run(challenger=True)
    assert keys.created and keys.revoked
    assert result["token_limit"] == 525
    assert result["cap_source"] == "baseline_usage_1_5x"
    assert backend.receipt[1]["result"]["token_limit"] == 525


def test_failed_baseline_uses_frozen_ceiling_and_missing_baseline_blocks_challenger():
    job = {"round_id": "week2", "stage": "week_one", "paper_id": "paper-1",
           "hotkey": "6" * 48, "package_sha256": "c" * 64}
    backend = Backend(job, challenger=True, baseline_status="package_failed", baseline_usage=100)
    assert paper_token_cap(job, backend=backend, published_ceiling=1000) == (
        1000, "published_ceiling_baseline_fault",
    )
    backend.list_paper_jobs = lambda *_: []
    with pytest.raises(ValueError, match="baseline must finish"):
        paper_token_cap(job, backend=backend, published_ceiling=1000)


def test_package_hash_mismatch_stops_before_key_creation():
    job = {"job_id": "a" * 64, "round_id": "week2", "hotkey": "5" * 48,
           "package_sha256": "b" * 64, "stage": "week_one", "paper_id": "paper-1", "lease_token": "lease"}
    keys = Keys()
    with pytest.raises(ValueError, match="sealed package"):
        run_claimed_job(
            job, backend=Backend(), worker_id="worker-1", network="testnet",
            fetch_package=lambda _job: b"wrong", fetch_pdf=lambda _job: b"%PDF",
            expected_pdf_sha256=hashlib.sha256(b"%PDF").hexdigest(),
            keys=keys, execution=Executor(), logs=Logs(),
            inference_base_url="http://gateway/v1", allowed_models=("model",),
            published_token_ceiling=1000, dollar_limit=1.0,
        )
    assert not keys.created


def test_claimed_job_uses_locked_pdf_source_before_worker_execution():
    package, pdf = b"package", b"%PDF sample"
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    job = {
        "job_id": "a" * 64, "round_id": "week2", "hotkey": signer.ss58_address,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "stage": "week_one", "paper_id": "paper-1", "lease_token": "lease-token",
        "status": "leased",
    }

    class LeasedBackend(Backend):
        def claim_job(self, round_id, *, worker_id, lease_seconds):
            assert (round_id, worker_id, lease_seconds) == ("week2", "worker-1", 3600)
            return job

    class PaperSources:
        def pdf_source_for_paper(self, stage, paper_id):
            assert (stage, paper_id) == ("week_one", "paper-1")
            return "s3://claims-papers/locked.pdf", hashlib.sha256(pdf).hexdigest()

        def paper_metadata_for_paper(self, stage, paper_id):
            assert (stage, paper_id) == ("week_one", "paper-1")
            return {"paper_id": paper_id, "title": "Paper"}

    class PdfFetcher:
        def fetch(self, url, digest):
            assert url == "s3://claims-papers/locked.pdf"
            assert digest == hashlib.sha256(pdf).hexdigest()
            return pdf

    backend = LeasedBackend(job)
    result = run_next_claimed_job(
        "week2", backend=backend, worker_id="worker-1", network="testnet",
        paper_sources=PaperSources(), pdf_fetcher=PdfFetcher(),
        fetch_package=lambda _: package,
        manifest_for_job=lambda _: {
            "schema_version": "claims_competition_manifest_v1", "language": "python",
            "package_format": "wheel",
            "package_filename": "claims_sample_miner-0.1.0-py3-none-any.whl", "source_included": True,
            "license_production_use": True, "entrypoint": "claims_sample_miner.cli:main",
            "models": ["example/model"],
        },
        keys=Keys(), execution=Executor(),
        logs=Logs(), inference_base_url="http://gateway.internal:8080/v1",
        allowed_models=("example/model",), published_token_ceiling=1000,
        dollar_limit=1.0,
    )
    assert result["status"] == "completed"
    assert backend.receipt[0] == job["job_id"]
