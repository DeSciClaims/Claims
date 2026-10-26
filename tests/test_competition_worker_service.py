from types import SimpleNamespace

import pytest

from competition.publication import canonical_sha256
from competition.policy_runtime import policy_runtime_config
from competition.worker_service import CompetitionWorkerCoordinator


def test_worker_coordinator_uses_only_frozen_round_policy(monkeypatch):
    worker_policy = {
        "allowed_models": ["qwen/qwen3-32b"],
        "published_token_ceiling": 100_000,
        "dollar_limit_usd": "1",
        "max_inference_output_tokens": 8192,
        "lease_seconds": 3600,
        "inference_base_url": "https://gateway.example/v1",
    }
    configuration = {"worker_policy": worker_policy,
                     "competition_policy_runtime": policy_runtime_config()}
    digest = canonical_sha256(configuration)

    class Backend:
        def get_round(self, _):
            return {"round_id": "week2", "network": "testnet",
                    "configuration_sha256": digest}

    backend = Backend()
    paper_inputs = SimpleNamespace(round_id="week2", backend=backend)
    class Packages:
        def __init__(self):
            self.backend = backend

        def manifest_for_job(self, _):
            return {}

        def __call__(self, _):
            return b""

    packages = Packages()
    policy = SimpleNamespace(round_id="week2", configuration_sha256=digest,
                             configuration=configuration)
    coordinator = CompetitionWorkerCoordinator(
        round_id="week2", network="testnet", worker_id="worker-a",
        backend=backend, paper_inputs=paper_inputs, packages=packages,
        evaluation=policy, pdf_fetcher=object(), keys=object(),
        execution=object(), logs=object(),
    )
    seen = []
    monkeypatch.setattr("competition.worker_service.run_next_claimed_job",
                        lambda *args, **kwargs: seen.append((args, kwargs)) or None)
    assert coordinator.run_once() is None
    assert seen[0][0] == ("week2",)
    assert seen[0][1]["allowed_models"] == ("qwen/qwen3-32b",)
    assert seen[0][1]["published_token_ceiling"] == 100_000
    assert seen[0][1]["lease_seconds"] == 3600
    assert seen[0][1]["inference_base_url"] == "https://gateway.example/v1"
    configuration["worker_policy"] = {**worker_policy, "published_token_ceiling": 1}
    with pytest.raises(ValueError, match="frozen round"):
        coordinator.run_once()
    assert len(seen) == 1
