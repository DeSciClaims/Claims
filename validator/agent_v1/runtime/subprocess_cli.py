from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from miner.agent_v1.runtime.usage import empty_usage, usage_from_cli_process
from miner.agent_v1.skillpack import SkillPack

from ..config import AgentV1ValidatorConfig
from ..models import RigorAgentRequest, RigorAgentResult


class SubprocessRigorRuntime:
    runtime_name = "agent-cli"

    def __init__(self, config: AgentV1ValidatorConfig) -> None:
        self.config = config

    def run_rigor(self, *, skill_pack: SkillPack, run_dir: Path, request: RigorAgentRequest) -> RigorAgentResult:
        if not self.config.cli_command:
            raise RuntimeError("SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND is required for validator agent-cli runtime.")
        started = time.time()
        output_path = run_dir / request.expected_output_path
        command = [
            *self.config.cli_command,
            "--run-dir",
            str(run_dir),
            "--skill-dir",
            str(skill_pack.root_dir),
            "--request",
            str(run_dir / "request.json"),
            "--output",
            str(output_path),
        ]
        command = _resolve_executable(command)
        try:
            completed = subprocess.run(
                command,
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
                env=_subprocess_env(str(self.config.base_dir)),
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.time() - started, 3)
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            manifest = _manifest(self, command, "timeout", elapsed, empty_usage("cli_unavailable"), skill_pack, output_path)
            _write_runtime_files(run_dir, manifest, stdout, stderr)
            raise RuntimeError(f"validator agent-cli runtime timed out after {self.config.timeout_seconds}s") from exc
        elapsed = round(time.time() - started, 3)
        manifest = _manifest(
            self,
            command,
            completed.returncode,
            elapsed,
            usage_from_cli_process(command, completed.stdout, completed.stderr),
            skill_pack,
            output_path,
        )
        if completed.returncode != 0:
            _write_runtime_files(run_dir, manifest, completed.stdout, completed.stderr)
            raise RuntimeError(f"validator agent-cli runtime failed with exit code {completed.returncode}")
        if not output_path.exists():
            _write_runtime_files(run_dir, manifest, completed.stdout, completed.stderr)
            raise RuntimeError(f"validator agent-cli runtime did not produce {output_path}")
        return RigorAgentResult(output_path=str(output_path), stdout=completed.stdout, stderr=completed.stderr, manifest=manifest)


def _manifest(runtime: SubprocessRigorRuntime, command, returncode, elapsed, usage, skill_pack, output_path) -> dict:
    return {
        "runtime": runtime.runtime_name,
        "runtime_alias": runtime.config.runtime,
        "command": command,
        "returncode": returncode,
        "elapsed_seconds": elapsed,
        "usage": usage,
        "skill": skill_pack.manifest(),
        "output_exists": output_path.exists(),
    }


def _write_runtime_files(run_dir: Path, manifest: dict, stdout: str, stderr: str) -> None:
    (run_dir / "rigor_backend_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "rigor_backend_stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "rigor_backend_stderr.txt").write_text(stderr, encoding="utf-8")


def _resolve_executable(command: list[str]) -> list[str]:
    if not command:
        return command
    executable = Path(command[0])
    if executable.is_absolute():
        return command
    if executable.exists():
        return [str(executable.absolute()), *command[1:]]
    return command


def _subprocess_env(base_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = base_dir if not existing else f"{base_dir}{os.pathsep}{existing}"
    return env
