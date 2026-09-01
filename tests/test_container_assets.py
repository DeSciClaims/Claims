from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


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


def test_container_entrypoint_configures_chutes_without_persisting_its_key() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert (
        'config set providers.chutes.base_url "${hermes_base_url:-https://llm.chutes.ai/v1}"'
        in entrypoint
    )
    assert 'config set providers.chutes.key_env "CHUTES_API_KEY"' in entrypoint
    assert "providers.chutes.api_key" not in entrypoint
    assert (
        'base_url = base_url or setting("CLAIMS_RIGOR_API_BASE") or setting("OPENROUTER_API_BASE")'
        not in entrypoint
    )


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
    assert "--workload-uid" in completed.stdout
    assert "--role" in completed.stdout
    assert "--leave-idle" in completed.stdout
    assert "automatic node startup" in completed.stdout


def test_targon_bootstrap_accepts_chutes_as_the_provider_key(tmp_path) -> None:
    module = _load_targon_bootstrap()
    wallet_dir = tmp_path / "validator-wallet"
    hotkey_dir = wallet_dir / "hotkeys"
    hotkey_dir.mkdir(parents=True)
    (hotkey_dir / "default").write_text("encrypted-hotkey", encoding="utf-8")
    env_file = tmp_path / "validator.env"
    env_file.write_text(
        "BT_WALLET_NAME=validator-wallet\n"
        "BT_WALLET_HOTKEY=default\n"
        "CHUTES_API_KEY=test-chutes-key\n",
        encoding="utf-8",
    )
    private_key = tmp_path / "deploy-key"
    public_key = tmp_path / "deploy-key.pub"
    private_key.write_text("private", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")

    inputs = module.validate_local_inputs(
        SimpleNamespace(
            env_file=str(env_file),
            wallet_dir=str(wallet_dir),
            ssh_private_key=str(private_key),
            ssh_public_key=str(public_key),
            role="validator",
            allow_missing_provider_key=False,
        )
    )

    assert inputs.wallet_name == "validator-wallet"


def test_targon_bootstrap_can_leave_configured_workload_idle() -> None:
    module = _load_targon_bootstrap()

    args = module.build_parser().parse_args(
        [
            "--role",
            "validator",
            "--name",
            "claims-validator-mainnet",
            "--image",
            "ghcr.io/desciclaims/claims-validator:abc1234",
            "--resource-name",
            "cpu-large",
            "--env-file",
            ".validator.mainnet.env",
            "--wallet-dir",
            ".runtime-wallets/claims-owner-vali",
            "--ssh-private-key",
            "~/.ssh/id_ed25519",
            "--leave-idle",
        ]
    )

    assert args.leave_idle is True


def test_targon_bootstrap_reuses_only_compatible_inactive_rental() -> None:
    module = _load_targon_bootstrap()
    args = type(
        "Args",
        (),
        {"workload_uid": "wrk-existing", "resource_name": "cpu-large"},
    )()

    module.validate_reusable_workload(
        args,
        {
            "type": "RENTAL",
            "state": {"status": "suspended"},
            "resource": {"name": "cpu-large"},
        },
    )

    for workload in (
        {
            "type": "RENTAL",
            "state": {"status": "running"},
            "resource": {"name": "cpu-large"},
        },
        {
            "type": "RENTAL",
            "state": {"status": "suspended"},
            "resource": {"name": "cpu-medium"},
        },
        {
            "type": "VM",
            "state": {"status": "suspended"},
            "resource": {"name": "cpu-large"},
        },
    ):
        try:
            module.validate_reusable_workload(args, workload)
        except module.BootstrapError:
            pass
        else:
            raise AssertionError(f"unsafe workload was accepted: {workload}")


def test_targon_reuse_update_clears_old_command_and_starts_idle() -> None:
    module = _load_targon_bootstrap()
    payload = {
        "name": "claims-validator-mainnet",
        "image": "ghcr.io/desciclaims/claims-validator:abc1234",
        "resource_name": "cpu-large",
        "type": "RENTAL",
        "args": ["idle"],
        "envs": [{"name": "CLAIMS_BRONZE_ROOT", "value": "/data/bronze"}],
        "ports": [],
        "ssh_keys": ["shk-123"],
        "volumes": [{"uid": "vol-123", "mount_path": "/data", "read_only": False}],
    }

    update = module.build_idle_workload_update(payload)

    assert update["commands"] == []
    assert update["args"] == ["idle"]
    assert update["volumes"] == payload["volumes"]
    assert "resource_name" not in update
    assert "type" not in update


def test_targon_reuse_detects_attached_data_volume() -> None:
    module = _load_targon_bootstrap()

    assert module.attached_data_volume_uid(
        {
            "volumes": [
                {
                    "uid": "vol-existing",
                    "name": "claims-validator-mainnet-data",
                    "mount_path": "/data",
                }
            ]
        }
    ) == "vol-existing"
    assert module.attached_data_volume_uid({"volumes": None}) == ""


def test_targon_ssh_key_discovery_uses_org_scoped_v3_endpoint(tmp_path) -> None:
    module = _load_targon_bootstrap()
    public_key = tmp_path / "claims-deploy.pub"
    public_key.write_text("ssh-rsa existing-key claims-deploy\n")
    inputs = module.LocalInputs(
        env_file=tmp_path / "validator.env",
        wallet_dir=tmp_path / "wallet",
        wallet_name="claims-owner-vali",
        hotkey_name="owner-hk-01",
        ssh_private_key=tmp_path / "claims-deploy",
        ssh_public_key=public_key,
    )

    class FakeApi:
        def org_path(self, suffix: str) -> str:
            return f"/tha/v3/orgs/claims/{suffix}"

        def request(self, method: str, path: str, payload=None):
            assert method == "GET"
            assert path == "/tha/v3/orgs/claims/ssh-keys?limit=1000"
            assert payload is None
            return {
                "items": [
                    {
                        "uid": "shk-existing",
                        "public_key_raw": "ssh-rsa existing-key claims-deploy",
                    }
                ]
            }

    assert module.ensure_ssh_key(FakeApi(), inputs, "") == "shk-existing"


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
