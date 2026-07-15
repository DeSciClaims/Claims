from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import AgentRequest, AgentResult
from .usage import empty_usage, usage_from_cli_process
from ..config import AgentV1Config
from ..skillpack import SkillPack


class SubprocessAgentRuntime:
    runtime_name = "agent-cli"

    def __init__(self, config: AgentV1Config) -> None:
        self.config = config

    def run_skill(self, *, skill_pack: SkillPack, run_dir: Path, request: AgentRequest) -> AgentResult:
        if not self.config.cli_command:
            raise RuntimeError(
                "SUBNET_CLAIMS_AGENT_CLI_COMMAND is required for CLI agent runtimes. "
                "The command receives --run-dir, --skill-dir, --request, and --output arguments."
            )
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
            _write_failure_manifest(
                run_dir,
                {
                    "runtime": self.runtime_name,
                    "runtime_alias": self.config.runtime,
                    "command": command,
                    "returncode": "timeout",
                    "elapsed_seconds": elapsed,
                    "timeout_seconds": self.config.timeout_seconds,
                    "usage": empty_usage("cli_unavailable"),
                    "skill": skill_pack.manifest(),
                    "output_exists": output_path.exists(),
                },
                stdout,
                stderr,
            )
            raise RuntimeError(
                f"agent-cli runtime timed out after {self.config.timeout_seconds}s "
                f"(elapsed={elapsed}s command={command!r})"
            ) from exc
        except OSError as exc:
            elapsed = round(time.time() - started, 3)
            _write_failure_manifest(
                run_dir,
                {
                    "runtime": self.runtime_name,
                    "runtime_alias": self.config.runtime,
                    "command": command,
                    "returncode": "startup_error",
                    "elapsed_seconds": elapsed,
                    "usage": empty_usage("cli_unavailable"),
                    "skill": skill_pack.manifest(),
                    "output_exists": output_path.exists(),
                    "error": str(exc),
                },
                "",
                str(exc),
            )
            raise RuntimeError(f"agent-cli runtime failed to start: {exc}") from exc
        elapsed = round(time.time() - started, 3)
        manifest = {
            "runtime": self.runtime_name,
            "runtime_alias": self.config.runtime,
            "command": command,
            "returncode": completed.returncode,
            "elapsed_seconds": elapsed,
            "usage": usage_from_cli_process(command, completed.stdout, completed.stderr),
            "skill": skill_pack.manifest(),
            "output_exists": output_path.exists(),
        }
        if completed.returncode != 0:
            _write_failure_manifest(run_dir, manifest, completed.stdout, completed.stderr)
            raise RuntimeError(f"agent-cli runtime failed with exit code {completed.returncode}")
        if not output_path.exists():
            _write_failure_manifest(run_dir, manifest, completed.stdout, completed.stderr)
            raise RuntimeError(f"agent-cli runtime did not produce {output_path}")
        return AgentResult(
            output_path=output_path,
            stdout=completed.stdout,
            stderr=completed.stderr,
            manifest=manifest,
        )


def _write_failure_manifest(run_dir: Path, manifest: dict, stdout: str, stderr: str) -> None:
    (run_dir / "backend_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "backend_stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "backend_stderr.txt").write_text(stderr, encoding="utf-8")


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
