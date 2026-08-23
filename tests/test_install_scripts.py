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
