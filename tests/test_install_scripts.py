from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_role_installers_expose_help_without_installing() -> None:
    for name in ("install-miner.sh", "install-validator.sh"):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / name), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--skip-system-packages" in result.stdout
        assert "Hermes is the only external CLI harness installed automatically" in result.stdout
        assert "never creates, copies, registers, or funds" in result.stdout


def test_validator_installer_uses_public_reference_repository() -> None:
    installer = (ROOT / "scripts" / "install-node.sh").read_text(encoding="utf-8")
    assert "https://github.com/DeSciClaims/claims-reference-miner.git" in installer
    assert "--reference-repo-version" in installer
    assert "--reference-key" not in installer
    assert "GIT_SSH_COMMAND" not in installer


def test_hermes_install_skips_optional_browser_engine() -> None:
    installer = (ROOT / "scripts" / "install-node.sh").read_text(encoding="utf-8")
    assert 'bash "${installer}" --skip-setup --skip-browser' in installer


def test_hermes_install_configures_provider_without_interactive_setup() -> None:
    installer = (ROOT / "scripts" / "install-node.sh").read_text(encoding="utf-8")
    assert 'config set model.provider "${hermes_provider}"' in installer
    assert 'config set model.default "${hermes_model}"' in installer
    assert 'config set model.base_url "${hermes_base_url}"' in installer
    for role in ("miner", "validator"):
        template = (ROOT / "examples" / f"{role}.env.example").read_text(encoding="utf-8")
        assert "HERMES_PROVIDER=openrouter" in template
        assert "HERMES_MODEL=deepseek/deepseek-v4-flash" in template
        assert "HERMES_BASE_URL=https://openrouter.ai/api/v1" in template


def test_validator_template_uses_scheduled_weight_submitting_runs() -> None:
    template = (ROOT / "examples" / "validator.env.example").read_text(encoding="utf-8")
    assert "CLAIMS_AUDIT_ONLY=false" in template
    assert "CLAIMS_MAX_STEPS=4" in template
    assert "CLAIMS_QUERY_INTERVAL=21600" in template
    assert "CLAIMS_MINER_SELECTION_MODE=adaptive" in template
    assert "CLAIMS_AUDIT_METHOD=llm" in template
    assert "CLAIMS_SILVER_ADJUDICATION_HARNESS=hermes-cli" in template
    assert "CLAIMS_SILVER_FILE_AGENT_FALLBACK=none" in template


def test_detailed_testnet_profile_is_runnable_and_uses_distinct_models() -> None:
    profile = (ROOT / "validator" / "agent_v1" / "validator.testnet.env.example").read_text(
        encoding="utf-8"
    )

    assert "BT_WALLET_NAME=claims-test-validator" in profile
    assert "CLAIMS_BACKEND_URL=https://api.claims111.ai" in profile
    assert "CLAIMS_BATCH_SIZE=50" in profile
    assert "CLAIMS_TIMEOUT=3600" in profile
    assert "CLAIMS_MINER_SELECTION_MODE=adaptive" in profile
    assert "CLAIMS_MINER_SAMPLE_SIZE=10" in profile
    assert "\nCLAIMS_TARGET_UIDS=" not in profile
    assert "CLAIMS_MAX_STEPS=1" in profile
    assert "CLAIMS_SILVER_WORKFLOW_MODE=file-agent" in profile
    assert "CLAIMS_SILVER_ADJUDICATION_MAX_IN_FLIGHT=32" in profile
    assert "CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES=true" in profile
    assert "CLAIMS_RIGOR_MODEL=openai/gpt-4o-mini" in profile
    assert "CLAIMS_SILVER_ADJUDICATION_MODEL_A=deepseek/deepseek-v4-flash" in profile
    assert "CLAIMS_SILVER_ADJUDICATION_MODEL_B=qwen/qwen3.7-flash" in profile


def test_mainnet_profile_has_production_policy_without_prescribed_models() -> None:
    profile = (ROOT / "validator" / "agent_v1" / "validator.mainnet.env.example").read_text(
        encoding="utf-8"
    )

    assert "BT_WALLET_NAME=\n" in profile
    assert "BT_NETUID=111" in profile
    assert "BT_SUBTENSOR_NETWORK=finney" in profile
    assert "CLAIMS_NETWORK=mainnet" in profile
    assert "CLAIMS_BATCH_SIZE=50" in profile
    assert "CLAIMS_MINER_SELECTION_MODE=adaptive" in profile
    assert "CLAIMS_MINER_SAMPLE_SIZE=10" in profile
    assert "CLAIMS_TIMEOUT=3600" in profile
    assert "CLAIMS_MAX_STEPS=4" in profile
    assert "CLAIMS_QUERY_INTERVAL=21600" in profile
    assert "CLAIMS_AUDIT_ONLY=false" in profile
    assert "CLAIMS_SILVER_WORKFLOW_MODE=file-agent" in profile
    assert "CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES=true" in profile
    assert "\nCLAIMS_TARGET_UIDS=" not in profile
    for key in (
        "HERMES_MODEL",
        "CLAIMS_RIGOR_MODEL",
        "CLAIMS_REFERENCE_MINER_MODEL",
        "CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL",
        "CLAIMS_SILVER_ADJUDICATION_MODEL_A",
        "CLAIMS_SILVER_ADJUDICATION_MODEL_B",
        "CLAIMS_SILVER_ADJUDICATION_TIEBREAK_MODEL",
        "CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL",
        "CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL",
        "CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL",
        "CLAIMS_SILVER_IMPORTANCE_MODEL",
    ):
        assert f"{key}=\n" in profile


def test_installation_docs_link_validator_profiles_and_docker_path() -> None:
    main_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    validator_readme = (ROOT / "validator" / "agent_v1" / "README.md").read_text(
        encoding="utf-8"
    )

    for content in (main_readme, validator_readme):
        assert "validator.testnet.env.example" in content
        assert "validator.mainnet.env.example" in content
    assert "### Manual Installation" in main_readme
    assert "### Ubuntu Installers" in main_readme
    assert "### Docker" in main_readme
    assert "CLAIMS_BACKEND_URL=https://api.claims111.ai" in main_readme
    assert "CLAIMS_BACKEND_URL=https://artifacts.claims111.ai" in main_readme
