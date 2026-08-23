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


def test_reference_access_helper_exposes_public_key_contract() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "prepare-reference-access.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Only the public key should be shared" in result.stdout


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
