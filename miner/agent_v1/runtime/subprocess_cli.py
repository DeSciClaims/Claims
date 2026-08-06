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
            manifest = {
                "runtime": self.runtime_name,
                "runtime_alias": self.config.runtime,
                "harness": _harness_from_agent_cli(command),
                "command": command,
                "model": _model_from_agent_cli(command),
                "returncode": "timeout",
                "elapsed_seconds": elapsed,
                "timeout_seconds": self.config.timeout_seconds,
                "usage": usage_from_cli_process(command, stdout, stderr),
                "skill": skill_pack.manifest(),
                "output_exists": output_path.exists(),
            }
            if _output_is_valid_json(output_path):
                manifest["output_recovered"] = True
                manifest["recovery_reason"] = "timeout_with_valid_output"
                _write_runtime_artifacts(run_dir, manifest, stdout, stderr)
                return AgentResult(
                    output_path=output_path,
                    stdout=stdout,
                    stderr=stderr,
                    manifest=manifest,
                )
            _write_runtime_artifacts(run_dir, manifest, stdout, stderr)
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
        model = _model_from_agent_cli(command)
        harness = _harness_from_agent_cli(command)
        manifest = {
            "runtime": self.runtime_name,
            "runtime_alias": self.config.runtime,
            "harness": harness,
            "command": command,
            "model": model,
            "returncode": completed.returncode,
            "elapsed_seconds": elapsed,
            "usage": usage_from_cli_process(command, completed.stdout, completed.stderr),
            "skill": skill_pack.manifest(),
            "output_exists": output_path.exists(),
        }
        if completed.returncode != 0:
            if _output_is_valid_json(output_path):
                manifest["output_recovered"] = True
                manifest["recovery_reason"] = "nonzero_exit_with_valid_output"
                _write_runtime_artifacts(run_dir, manifest, completed.stdout, completed.stderr)
                return AgentResult(
                    output_path=output_path,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    manifest=manifest,
                )
            _write_runtime_artifacts(run_dir, manifest, completed.stdout, completed.stderr)
            raise RuntimeError(f"agent-cli runtime failed with exit code {completed.returncode}")
        if not output_path.exists():
            _write_runtime_artifacts(run_dir, manifest, completed.stdout, completed.stderr)
            raise RuntimeError(f"agent-cli runtime did not produce {output_path}")
        _write_runtime_artifacts(run_dir, manifest, completed.stdout, completed.stderr)
        return AgentResult(
            output_path=output_path,
            stdout=completed.stdout,
            stderr=completed.stderr,
            manifest=manifest,
        )


def _write_failure_manifest(run_dir: Path, manifest: dict, stdout: str, stderr: str) -> None:
    _write_runtime_artifacts(run_dir, manifest, stdout, stderr)


def _write_runtime_artifacts(run_dir: Path, manifest: dict, stdout: str, stderr: str) -> None:
    (run_dir / "backend_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "backend_stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "backend_stderr.txt").write_text(stderr, encoding="utf-8")


def _output_is_valid_json(output_path: Path) -> bool:
    if not output_path.exists():
        return False
    try:
        json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return True


def _resolve_executable(command: list[str]) -> list[str]:
    if not command:
        return command
    executable = Path(command[0])
    if executable.is_absolute():
        return command
    if executable.exists():
        return [str(executable.absolute()), *command[1:]]
    return command


def _model_from_agent_cli(command: list[str]) -> str:
    candidates = [
        _split_command(os.getenv("CLAIMS_AGENT_INNER_COMMAND", "")),
        _split_command(os.getenv("SUBNET_CLAIMS_AGENT_CLI_COMMAND", "")),
        command,
    ]
    for candidate in candidates:
        model = _model_from_command(candidate)
        if model:
            return model
    configured = os.getenv("SUBNET_CLAIMS_AGENT_MODEL") or os.getenv("OPENROUTER_MODEL")
    return _model_id_or_empty(configured)


def _harness_from_agent_cli(command: list[str]) -> str:
    candidates = [
        _split_command(os.getenv("CLAIMS_AGENT_INNER_COMMAND", "")),
        _split_command(os.getenv("SUBNET_CLAIMS_AGENT_CLI_COMMAND", "")),
        command,
    ]
    for candidate in candidates:
        harness = _harness_from_command(candidate)
        if harness:
            return harness
    return "agent-cli"


def _model_from_command(command: list[str]) -> str:
    for index, part in enumerate(command):
        if part in {"-m", "--model"} and index + 1 < len(command):
            if _is_python_module_flag(command, index):
                continue
            return _model_id_or_empty(command[index + 1])
        if part.startswith("--model="):
            return _model_id_or_empty(part.split("=", 1)[1])
    return ""


def _harness_from_command(command: list[str]) -> str:
    if not command:
        return ""
    lowered = [Path(part).name.lower() for part in command]
    joined = " ".join(str(part).lower() for part in command)
    if any(part in {"hermes", "hermes-agent"} for part in lowered) or "hermes_prompt" in joined:
        return "hermes-cli"
    if any(part in {"codex", "codex-cli"} for part in lowered) or "codex_prompt" in joined:
        return "codex-cli"
    if any(part in {"claude", "claude-code", "claude-cli"} for part in lowered):
        return "claude-cli"
    return ""


def _is_python_module_flag(command: list[str], index: int) -> bool:
    if command[index] != "-m" or index == 0:
        return False
    executable = Path(command[index - 1]).name
    if executable.startswith("python"):
        return True
    return index == 1 and Path(command[0]).name.startswith("python")


def _split_command(command: str) -> list[str]:
    if not command.strip():
        return []
    try:
        import shlex

        return shlex.split(command)
    except ValueError:
        return command.split()


def _model_id_or_empty(value: str | None) -> str:
    model = str(value or "").strip()
    if not model:
        return ""
    lower = model.lower()
    runtime_aliases = {
        "agent-cli",
        "claude-cli",
        "codex-cli",
        "hermes-cli",
        "hermes-agent",
        "cli",
        "dspy-react",
        "langchain-agent",
        "local_manifest",
        "static",
    }
    if lower in runtime_aliases:
        return ""
    if lower.startswith(("miner.", "neurons.", "claims_reference_miner")):
        return ""
    if ".wrappers." in lower:
        return ""
    return model


def _subprocess_env(base_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = base_dir if not existing else f"{base_dir}{os.pathsep}{existing}"
    return env
