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
            "package_filename": "claims_sample_miner-0.1.0-py3-none-any.whl",
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
    def __init__(self, over=False, expected_tokens=1000, expected_dollars=1.0):
        self.over = over
        self.revoked = False
        self.created = False
        self.expected_tokens = expected_tokens
        self.expected_dollars = expected_dollars

    def create_paper_key(self, **kwargs):
        self.created = True
        assert kwargs["allowed_models"] == ("example/model",)
        assert kwargs["token_limit"] == self.expected_tokens
        assert kwargs["dollar_limit"] == pytest.approx(self.expected_dollars)
        return PaperKey("key", TOKEN, datetime.now(UTC) + timedelta(minutes=10),
                        self.expected_tokens, kwargs["dollar_limit"])

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
        assert self.keys.revoked or not self.keys.created
        self.receipt = job_id, kwargs


def _run(*, over=False, artifact=None, lease=None, active=None, execution=None,
         is_current_release=None, keys=None):
    active = active or _active()
    lease = lease or _lease(active)
    keys = keys or Keys(over)
    logs = Logs()
    results = Results(keys)
    runner = execution or Execution(artifact)
    result = run_production_paper(
        active, lease, network="testnet", worker_id="production-worker-1",
        fetch_pdf=lambda url, digest: PDF,
        keys=keys, execution=runner, logs=logs, results=results,
        inference_base_url="https://inference.example/v1",
        allowed_models=("example/model",), token_limit=1000, dollar_limit=1.0,
        is_current_release=is_current_release,
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


def test_production_retry_uses_only_recorded_remaining_budget():
    active = _active()
    lease = _lease(active)
    lease["attempts"] = 2
    lease["result_private"] = {
        "status": "infrastructure_fault", "billing_observed": True,
        "prompt_and_completion_tokens": 300, "inference_cost_usd": 0.2,
    }
    result, keys, _, _, execution = _run(
        active=active, lease=lease,
        keys=Keys(expected_tokens=700, expected_dollars=0.8),
    )
    assert keys.created and keys.revoked and execution.calls == 1
    assert result["token_limit"] == 700
    assert result["paper_token_limit"] == 1000


@pytest.mark.parametrize("prior, expected", [
    ({}, "unmetered_prior_attempt"),
    ({"status": "infrastructure_fault", "billing_observed": True,
      "prompt_and_completion_tokens": 1000, "inference_cost_usd": 0.2},
     "prior_attempt_exhausted_budget"),
])
def test_production_retry_without_allowance_never_runs_miner(prior, expected):
    active = _active()
    lease = _lease(active)
    lease["attempts"] = 2
    lease["result_private"] = prior
    result, keys, logs, receipts, execution = _run(active=active, lease=lease)
    assert not keys.created and execution.calls == 0
    assert result["detail"] == expected
    assert result["status"] == ("infrastructure_fault" if not prior else "cap_exceeded")
    assert receipts.receipt[1]["result"] == result
    assert expected.encode() in logs.receipt[1]


def test_production_discards_result_when_handoff_changes_during_execution():
    checks = iter((True, False))
    with pytest.raises(ValueError, match="release changed"):
        _run(is_current_release=lambda: next(checks))
