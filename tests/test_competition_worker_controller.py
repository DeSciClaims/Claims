import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from bittensor_wallet import Keypair

from competition.bifrost import PaperKey
from competition.worker_controller import ExecutionOutput, run_claimed_job


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
    def __init__(self, over=False):
        self.over = over
        self.revoked = False
        self.created = False

    def create_paper_key(self, **kwargs):
        self.created = True
        assert kwargs["token_limit"] == 1000
        return PaperKey("key-1", "sk-bf-short-lived", datetime.now(UTC) + timedelta(minutes=10), 1000)

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
    def __init__(self):
        self.receipt = None

    def complete_job(self, job_id, **kwargs):
        self.receipt = (job_id, kwargs)
        return {"status": "completed"}


def run(over=False, package=b"package", pdf=b"%PDF sample"):
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    job = {
        "job_id": "a" * 64, "round_id": "week2", "hotkey": signer.ss58_address,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "paper_id": "paper-1", "lease_token": "lease-token",
    }
    keys, logs, backend = Keys(over), Logs(), Backend()
    result = run_claimed_job(
        job, backend=backend, worker_id="worker-1", network="testnet",
        fetch_package=lambda _job: package, fetch_pdf=lambda _job: pdf,
        expected_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        keys=keys, execution=Executor(), logs=logs,
        inference_base_url="http://gateway.internal:8080/v1",
        allowed_models=("example/model",), token_limit=1000, dollar_limit=1.0,
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


def test_package_hash_mismatch_stops_before_key_creation():
    job = {"job_id": "a" * 64, "round_id": "week2", "hotkey": "5" * 48,
           "package_sha256": "b" * 64, "paper_id": "paper-1", "lease_token": "lease"}
    keys = Keys()
    with pytest.raises(ValueError, match="sealed package"):
        run_claimed_job(
            job, backend=Backend(), worker_id="worker-1", network="testnet",
            fetch_package=lambda _job: b"wrong", fetch_pdf=lambda _job: b"%PDF",
            expected_pdf_sha256=hashlib.sha256(b"%PDF").hexdigest(),
            keys=keys, execution=Executor(), logs=Logs(),
            inference_base_url="http://gateway/v1", allowed_models=("model",),
            token_limit=1000, dollar_limit=1.0,
        )
    assert not keys.created
