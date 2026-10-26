"""One-paper worker harness, to run only inside a disposable miner VM.

The trusted controller stages a sealed package, PDF, request, and short-lived
Bifrost virtual key, then starts this harness on a VM with no infrastructure
credentials. Network and filesystem isolation are enforced by the VM image
and host firewall, not by this Python process.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from miner.agent_v1.artifact_models import Artifact
from miner.agent_v1.artifact_validator import validate_agent_artifact


@dataclass(frozen=True)
class WorkerResult:
    status: str
    output_path: Path
    log_path: Path
    elapsed_seconds: float
    detail: str = ""


def _phase(
    command: list[str], *, cwd: Path, env: dict[str, str], log,
    timeout_seconds: float, label: str,
) -> tuple[int | None, float]:
    log.write(f"\n[{label}] start: {shlex.join(command)}\n".encode())
    log.flush()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        log.write(f"\n[{label}] timeout after {elapsed:.2f}s\n".encode())
        log.flush()
        return None, elapsed
    elapsed = time.monotonic() - started
    log.write(f"\n[{label}] exit={completed.returncode} elapsed={elapsed:.2f}s\n".encode())
    log.flush()
    return completed.returncode, elapsed


def run_one_paper(
    *, package_path: Path, entrypoint: str, request_path: Path,
    output_path: Path, work_dir: Path, log_path: Path, wheelhouse: Path,
    inference_base_url: str, paper_key: str,
    install_timeout_seconds: int = 600, run_timeout_seconds: int = 1800,
) -> WorkerResult:
    """Install an untrusted package offline and invoke its fixed entrypoint.

    This harness assumes the whole VM is discarded after the paper. The
    controller must independently validate the returned artifact and account
    for Bifrost usage after each call and after termination.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", entrypoint):
        raise ValueError("entrypoint must be module:function")
    if not package_path.is_file() or not package_path.name.endswith((".whl", ".tar.gz")):
        raise ValueError("package must be a wheel or source archive")
    if not request_path.is_file() or not wheelhouse.is_dir():
        raise ValueError("request and offline wheelhouse must exist")
    if not inference_base_url.startswith(("http://", "https://")) or not paper_key:
        raise ValueError("inference endpoint and paper key are required")
    if install_timeout_seconds < 1 or run_timeout_seconds < 1:
        raise ValueError("timeouts must be positive")
    package_path = package_path.resolve()
    request_path = request_path.resolve()
    wheelhouse = wheelhouse.resolve()
    work_dir = work_dir.resolve()
    output_path = output_path.resolve()
    log_path = log_path.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.defpath,
        "HOME": str(work_dir),
        "TMPDIR": str(work_dir),
        "LANG": "C.UTF-8",
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
    }
    venv_dir = work_dir / "venv"
    started = time.monotonic()
    install_deadline = started + install_timeout_seconds
    with log_path.open("wb") as log:
        code, _ = _phase(
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=work_dir, env=environment, log=log,
            timeout_seconds=install_timeout_seconds, label="venv",
        )
        if code != 0:
            return WorkerResult("build_failed", output_path, log_path, time.monotonic() - started, "venv creation failed")
        python = venv_dir / "bin" / "python"
        remaining_install = install_deadline - time.monotonic()
        if remaining_install <= 0:
            log.write(b"\n[install] shared build budget exhausted\n")
            return WorkerResult("build_failed", output_path, log_path,
                                time.monotonic() - started, "package install timed out")
        code, _ = _phase(
            [str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheelhouse), str(package_path)],
            cwd=work_dir, env=environment, log=log,
            timeout_seconds=remaining_install, label="install",
        )
        if code != 0:
            return WorkerResult("build_failed", output_path, log_path, time.monotonic() - started, "package install failed")
        module, function = entrypoint.split(":", 1)
        script = (
            "import importlib,sys; "
            "sys.argv=['miner','--request',sys.argv[1],'--output',sys.argv[2]]; "
            f"getattr(importlib.import_module({module!r}),{function!r})()"
        )
        run_environment = {
            **environment,
            "CLAIMS_INFERENCE_BASE_URL": inference_base_url,
            "CLAIMS_INFERENCE_TOKEN": paper_key,
        }
        code, _ = _phase(
            [str(python), "-c", script, str(request_path), str(output_path)],
            cwd=work_dir, env=run_environment, log=log,
            timeout_seconds=run_timeout_seconds, label="execute",
        )
        if code != 0:
            status = "timeout" if code is None else "package_failed"
            return WorkerResult(status, output_path, log_path, time.monotonic() - started, "miner execution failed")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            artifact = Artifact.model_validate(payload)
            request = json.loads(request_path.read_text(encoding="utf-8"))
            issues = validate_agent_artifact(artifact)
            if artifact.paper.paper_id != str(request["paper"]["paper_id"]):
                issues.append({"message": "output paper ID differs from request"})
            if issues:
                raise ValueError("; ".join(item["message"] for item in issues))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.write(f"\n[validate] invalid output: {exc}\n".encode())
            return WorkerResult("invalid_output", output_path, log_path, time.monotonic() - started, "output schema invalid")
        log.write(b"\n[validate] agent_v1 output accepted\n")
    return WorkerResult("completed", output_path, log_path, time.monotonic() - started)
