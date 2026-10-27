import json

import pytest

from competition.pilot_worker_factory import _bifrost_admin_client, create_worker_coordinator
from competition.publication import canonical_sha256
from competition.policy_runtime import policy_runtime_config
from competition.ssh_worker import RestrictedSshWorkerTransport
from competition.targon_vms import PersistentVmExecution


def test_pilot_worker_factory_binds_all_adapters_to_frozen_rules(tmp_path, monkeypatch):
    configuration = {"competition_policy_runtime": policy_runtime_config(), "worker_policy": {
        "inference_base_url": "https://gateway.example/v1",
        "allowed_models": ["qwen/qwen3-32b"],
        "published_token_ceiling": 100_000,
        "dollar_limit_usd": "1",
        "max_inference_output_tokens": 8192,
        "lease_seconds": 3600,
    }}
    digest = canonical_sha256(configuration)
    document = {
        "round_id": "week1", "configuration_sha256": digest,
        "configuration": configuration,
        "baseline_upload": {"hotkey": "claims-reference", "package_sha256": "a" * 64,
                            "package_object_key": "packages/reference.whl"},
    }
    (tmp_path / "week1.json").write_text(json.dumps(document))
    identity = tmp_path / "identity"
    identity.write_text("fake private key")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("[203.0.113.1]:20005 ssh-ed25519 AAAA")
    settings = {
        "CLAIMS_COMPETITION_ROUNDS_DIR": str(tmp_path),
        "CLAIMS_HIPPIUS_ENDPOINT": "https://s3.hippius.com",
        "CLAIMS_HIPPIUS_ACCESS_KEY_ID": "id",
        "CLAIMS_HIPPIUS_SECRET_ACCESS_KEY": "secret",
        "CLAIMS_HIPPIUS_PACKAGE_BUCKET": "packages",
        "CLAIMS_HIPPIUS_LOG_BUCKET": "logs",
        "CLAIMS_COMPETITION_PDF_S3_BUCKETS": "papers",
        "CLAIMS_BIFROST_MANAGEMENT_URL": "http://127.0.0.1:8080",
        "CLAIMS_BIFROST_ADMIN_BEARER": "admin",
        "CLAIMS_TARGON_ORG_SLUG": "claims",
        "CLAIMS_TARGON_API_TOKEN": "targon",
        "CLAIMS_TARGON_WORKER_IMAGE": "worker-image",
        "CLAIMS_TARGON_RESOURCE_NAME": "gpu",
        "CLAIMS_TARGON_SSH_KEY_UID": "key-1",
        "CLAIMS_TARGON_MAX_HOURLY_USD": "10",
        "CLAIMS_WORKER_SSH_IDENTITY": str(identity),
        "CLAIMS_WORKER_SSH_KNOWN_HOSTS": str(known_hosts),
        "CLAIMS_WORKER_GATEWAY_IP": "203.0.113.10",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("competition.pilot_worker_factory.hippius_s3_client",
                        lambda **kwargs: object())
    backend = object()
    row = {"round_id": "week1", "network": "testnet",
           "configuration_sha256": digest, "reference_release_id": "bronze-1"}
    worker = create_worker_coordinator(row, backend, "worker-1")
    assert worker.paper_inputs.backend is backend
    assert worker.evaluation.configuration_sha256 == digest
    assert worker.execution.transport.inference_base_url == "https://gateway.example/v1"
    assert isinstance(worker.execution.transport, RestrictedSshWorkerTransport)
    assert worker.execution.transport.ssh_user == "claims-control"
    assert worker.execution.transport.gateway_port == 443
    assert worker.packages.baseline_upload == document["baseline_upload"]

    monkeypatch.setenv("CLAIMS_WORKER_VM_MODE", "persistent")
    monkeypatch.setenv("CLAIMS_WORKER_VM_UID", "miner-workers")
    monkeypatch.setenv("CLAIMS_WORKER_PUBLIC_IP", "203.0.113.1")
    monkeypatch.setenv("CLAIMS_WORKER_SSH_PORT", "20003")
    monkeypatch.setenv("CLAIMS_WORKER_HOURLY_USD", "1")
    for name in settings:
        if name.startswith("CLAIMS_TARGON_"):
            monkeypatch.delenv(name)
    persistent = create_worker_coordinator(row, backend, "worker-1")
    assert isinstance(persistent.execution, PersistentVmExecution)
    assert persistent.execution.vm.uid == "miner-workers"
    assert persistent.execution.vm.ssh_port == 20003

    document["configuration"]["worker_policy"]["published_token_ceiling"] = 1
    (tmp_path / "week1.json").write_text(json.dumps(document))
    with pytest.raises(ValueError, match="frozen hash"):
        create_worker_coordinator(row, backend, "worker-1")


def test_pilot_worker_factory_accepts_live_bifrost_basic_admin(monkeypatch):
    monkeypatch.setenv("CLAIMS_BIFROST_MANAGEMENT_URL", "http://127.0.0.1:8080")
    monkeypatch.delenv("CLAIMS_BIFROST_ADMIN_BEARER", raising=False)
    monkeypatch.setenv("CLAIMS_BIFROST_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("CLAIMS_BIFROST_ADMIN_PASSWORD", "secret")
    client = _bifrost_admin_client()
    assert client.admin_username == "admin"
    assert client.admin_password == "secret"
    assert client.admin_bearer is None
    monkeypatch.setenv("CLAIMS_BIFROST_ADMIN_BEARER", "other")
    with pytest.raises(ValueError, match="one Bifrost"):
        _bifrost_admin_client()
