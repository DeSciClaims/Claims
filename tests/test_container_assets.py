from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    guide = (ROOT / "docs" / "targon-containers.md").read_text()

    assert "claims-node miner" in guide
    assert "claims-node validator" in guide
    assert "mounted at `/data`" in guide
    assert "public validator image" in guide
