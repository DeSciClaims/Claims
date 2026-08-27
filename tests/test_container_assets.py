from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_targon_bootstrap() -> ModuleType:
    path = ROOT / "scripts" / "bootstrap_targon_node.py"
    spec = importlib.util.spec_from_file_location("bootstrap_targon_node", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_container_entrypoint_is_valid_bash() -> None:
    subprocess.run(
        ["bash", "-n", str(ROOT / "docker" / "entrypoint.sh")],
        check=True,
    )
    completed = subprocess.run(
        ["bash", str(ROOT / "docker" / "entrypoint.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "idle" in completed.stdout
    assert "validator" in completed.stdout


def test_container_entrypoint_loads_role_env_without_shell_evaluation() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()

    assert "from dotenv import dotenv_values" in entrypoint
    assert 'export "${key}"' in entrypoint
    assert 'source "${env_file}"' not in entrypoint


def test_container_has_separate_miner_and_validator_targets() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()

    assert "FROM claims-base AS miner" in dockerfile
    assert "FROM claims-base AS validator" in dockerfile
    assert "COPY --from=claims_reference_miner" in dockerfile
    assert "--skip-setup --skip-browser" in dockerfile
    assert any(line.strip() == "nano \\" for line in dockerfile.splitlines())
    assert "CLAIMS_REFERENCE_MINER_CLAIMS_REPO=/opt/claims" in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile


def test_container_context_excludes_runtime_secrets_and_state() -> None:
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert ".env" in ignored
    assert ".env.*" in ignored
    assert ".git" in ignored
    assert "outputs" in ignored
    assert "validator/agent_v1/bronze" in ignored


def test_targon_guide_documents_manual_and_automatic_modes() -> None:
    guide = (ROOT / "docs" / "0016-targon-container-deployment.md").read_text()

    assert "claims-node miner" in guide
    assert "claims-node validator" in guide
    assert "mounted at `/data`" in guide
    assert "public validator image" in guide
    assert "First-Time Bootstrap" in guide
    assert "bootstrap_targon_node.py" in guide
    assert "--role miner" in guide
    assert "BT_AXON_EXTERNAL_IP" in guide
    assert "never sent through the Targon" in guide


def test_targon_bootstrap_exposes_help_without_network_access() -> None:
    completed = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "bootstrap_targon_node.py"),
            "--help",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--wallet-dir" in completed.stdout
    assert "--volume-uid" in completed.stdout
    assert "--role" in completed.stdout
    assert "automatic node startup" in completed.stdout


def test_targon_workload_payloads_contain_no_private_node_material() -> None:
    module = _load_targon_bootstrap()
    validator_args = type(
        "Args",
        (),
        {
            "role": "validator",
            "name": "claims-validator",
            "image": "ghcr.io/desciclaims/claims-validator:abc1234",
            "resource_name": "cpu-large",
            "axon_port": 8091,
        },
    )()

    validator_payload = module.build_workload_payload(
        validator_args,
        volume_uid="vol-123",
        ssh_key_uid="shk-123",
    )
    miner_args = type(
        "Args",
        (),
        {
            "role": "miner",
            "name": "claims-miner",
            "image": "ghcr.io/desciclaims/claims-miner:abc1234",
            "resource_name": "cpu-large",
            "axon_port": 8091,
        },
    )()
    miner_payload = module.build_workload_payload(
        miner_args,
        volume_uid="vol-456",
        ssh_key_uid="shk-123",
    )
    encoded = f"{validator_payload}{miner_payload}"

    assert validator_payload["args"] == ["idle"]
    assert validator_payload["ports"] == []
    assert validator_payload["volumes"] == [
        {"uid": "vol-123", "mount_path": "/data", "read_only": False}
    ]
    assert miner_payload["ports"] == [
        {"port": 8091, "protocol": "TCP", "routing": "DIRECT"}
    ]
    assert miner_payload["envs"] == [
        {"name": "CLAIMS_MINER_OUTPUT_DIR", "value": "/data/outputs/miner"}
    ]
    miner_runtime = module.build_runtime_update(
        miner_args,
        state={"public_ip": "203.0.113.10"},
        workload_payload=miner_payload,
    )
    assert miner_runtime["args"] == ["miner", "--logging.debug"]
    assert {"name": "BT_AXON_EXTERNAL_IP", "value": "203.0.113.10"} in miner_runtime[
        "envs"
    ]
    assert "OPENROUTER_API_KEY" not in encoded
    assert "BT_WALLET" not in encoded
