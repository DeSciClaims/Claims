from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from competition.bifrost import PaperKey, PaperUsage
from competition.production_reader import ActiveProductionPackage
from competition.production_runner import run_production_paper
from competition.worker_controller import ExecutionOutput


PDF = b"%PDF production paper"
PACKAGE = b"sealed-package"
TOKEN = "sk-bf-short-lived"


def _artifact():
    return {
        "paper": {"paper_id": "paper-1", "title": "Paper"},
        "logic": {"claims": [{
            "claim_id": "C1", "statement": "Result", "conditions": "Study",
            "status": "supported", "falsification_criteria": "No result",
            "proof": ["E1"], "evidence_ids": ["EV1"],
        }], "experiments": [{
            "experiment_id": "E1", "title": "Test", "verifies": ["C1"],
            "setup": "Groups", "procedure": "Compare", "expected_outcome": "Result",
            "evidence_ids": ["EV1"],
        }]},
        "evidence": {"records": [{
            "evidence_id": "EV1", "title": "Result", "role": "support",
            "summary": "Result", "linked_claim_ids": ["C1"],
        }]},
        "trace": {"node_id": "root", "node_type": "extraction", "summary": "Result"},
        "src": {"environment": ["production"], "artifacts": []},
    }


def _active():
    at = datetime(2026, 11, 2, 9, tzinfo=UTC)
    return ActiveProductionPackage(
        "week2", "a" * 64, "miner", sha256(PACKAGE).hexdigest(),
        at, at, None, PACKAGE,
        {"schema_version": "claims_competition_manifest_v1",
         "language": "python", "package_format": "wheel",
         "source_included": True, "license_production_use": True,
         "entrypoint": "sample.cli:main", "models": ["example/model"]},
    )


def _lease(active):
    return {
        "job_id": "b" * 64, "paper_id": "paper-1",
        "paper": {"paper_id": "paper-1", "title": "Paper"},
        "source_url": "https://papers.example/paper.pdf",
        "source_sha256": sha256(PDF).hexdigest(),
        "receipt_sha256": active.receipt_sha256,
        "package_sha256": active.package_sha256,
        "lease_token": "lease-1", "attempts": 1,
    }


class Keys:
    def __init__(self, over=False):
        self.over = over
        self.revoked = False

    def create_paper_key(self, **kwargs):
        assert kwargs["allowed_models"] == ("example/model",)
        assert kwargs["token_limit"] == 1000
        return PaperKey("key", TOKEN, datetime.now(UTC) + timedelta(minutes=10),
                        1000, kwargs["dollar_limit"])

    def check_budget(self, key):
        return PaperUsage(1100 if self.over else 350, 0.12, self.over)

    def revoke(self, key):
        self.revoked = True


class Execution:
    def __init__(self, artifact=None):
        self.artifact = _artifact() if artifact is None else artifact
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        assert kwargs["paper_key"] == TOKEN
        assert kwargs["job"]["worker_contract"]["request"]["paper"]["paper_id"] == "paper-1"
        return ExecutionOutput("completed", self.artifact, b"run " + TOKEN.encode())


class Logs:
    def __init__(self):
        self.receipt = None

    def put(self, key, content):
        self.receipt = key, content


class Results:
    def __init__(self, keys):
        self.keys = keys
        self.receipt = None

    def complete_production_job(self, job_id, **kwargs):
        assert self.keys.revoked
        self.receipt = job_id, kwargs


def _run(*, over=False, artifact=None, lease=None, active=None, execution=None):
    active = active or _active()
    lease = lease or _lease(active)
    keys = Keys(over)
    logs = Logs()
    results = Results(keys)
    runner = execution or Execution(artifact)
    result = run_production_paper(
        active, lease, network="testnet",
        fetch_pdf=lambda url, digest: PDF,
        keys=keys, execution=runner, logs=logs, results=results,
        inference_base_url="https://inference.example/v1",
        allowed_models=("example/model",), token_limit=1000, dollar_limit=1.0,
    )
    return result, keys, logs, results, runner


def test_production_runs_one_package_without_scoring_and_redacts_key():
    result, keys, logs, results, execution = _run()
    assert result["status"] == "completed"
    assert result["artifact"]["paper"]["paper_id"] == "paper-1"
    assert result["receipt_sha256"] == "a" * 64
    assert result["prompt_and_completion_tokens"] == 350
    assert execution.calls == 1
    assert keys.revoked
    assert TOKEN.encode() not in logs.receipt[1]
    assert results.receipt[1]["result"] == result


def test_production_cap_and_bad_output_do_not_publish_artifact():
    capped, _, _, _, _ = _run(over=True)
    assert capped["status"] == "cap_exceeded"
    assert capped["artifact"] is None
    bad, _, _, _, _ = _run(artifact={"paper": {"paper_id": "another"}})
    assert bad["status"] == "invalid_output"
    assert bad["artifact"] is None


def test_production_rejects_release_or_pdf_mismatch_before_inference():
    active = _active()
    lease = _lease(active)
    lease["receipt_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="lease differs"):
        _run(active=active, lease=lease)
    lease = _lease(active)
    lease["source_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="PDF differs"):
        _run(active=active, lease=lease)


def test_production_transport_fault_revokes_key_and_records_fault():
    class Broken:
        def run(self, **kwargs):
            raise RuntimeError("private transport detail")

    result, keys, _, receipts, _ = _run(execution=Broken())
    assert keys.revoked
    assert result["status"] == "infrastructure_fault"
    assert result["artifact"] is None
    assert "private transport detail" not in str(receipts.receipt)
