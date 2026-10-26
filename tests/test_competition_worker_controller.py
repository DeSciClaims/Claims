import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from bittensor_wallet import Keypair

from competition.bifrost import PaperKey
from competition.worker_controller import ExecutionOutput, paper_token_cap, run_claimed_job


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
        return PaperKey("key-1", "sk-bf-short-lived", datetime.now(UTC) + timedelta(minutes=10), self.expected_cap)

    def check_budget(self, key):
        assert key.value == "sk-bf-short-lived"
        return (1200 if self.over else 350, self.over)

    def revoke(self, key):
        assert key.value == "sk-bf-short-lived"
        self.revoked = True


class Executor:
    def run(self, **kwargs):
        assert kwargs["paper_key"] == "sk-bf-short-lived"
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


def run(over=False, package=b"package", pdf=b"%PDF sample", challenger=False):
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    job = {
        "job_id": "a" * 64, "round_id": "week2", "hotkey": signer.ss58_address,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "stage": "week_one", "paper_id": "paper-1", "lease_token": "lease-token",
    }
    keys, logs, backend = Keys(over, expected_cap=525 if challenger else 1000), Logs(), Backend(job, challenger=challenger)
    result = run_claimed_job(
        job, backend=backend, worker_id="worker-1", network="testnet",
        fetch_package=lambda _job: package, fetch_pdf=lambda _job: pdf,
        expected_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        keys=keys, execution=Executor(), logs=logs,
        inference_base_url="http://gateway.internal:8080/v1",
        allowed_models=("example/model",), published_token_ceiling=1000, dollar_limit=1.0,
    )
    return result, keys, logs, backend, job


def test_worker_controller_revokes_key_before_private_log_and_completion():
    result, keys, logs, backend, job = run()
    assert result["status"] == "completed"
    assert result["prompt_and_completion_tokens"] == 350
    assert keys.revoked
    assert logs.key.startswith(f"logs/testnet/week2/{job['hotkey']}/")
    assert b"sk-bf-short-lived" not in logs.content
    assert b"[REDACTED_PAPER_KEY]" in logs.content
    assert backend.receipt[0] == job["job_id"]
    assert backend.receipt[1]["result"]["artifact"]["paper"]["paper_id"] == "paper-1"


def test_over_cap_output_is_not_scored_as_a_success():
    result, keys, _logs, backend, _job = run(over=True)
    assert keys.revoked
    assert result["status"] == "cap_exceeded"
    assert backend.receipt[1]["result"]["artifact"] is None


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
