from __future__ import annotations

import shutil
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


NATIVE_HARNESSES = {"dspy-react", "langchain-agent"}
CLI_HARNESSES = {"hermes-cli", "codex-cli", "claude-cli"}
SUPPORTED_HARNESSES = NATIVE_HARNESSES | CLI_HARNESSES


@dataclass(frozen=True)
class AgentHarnessProfile:
    harness: str
    model: str
    runtime: str
    cli_command: str = ""
    inner_command: str = ""


def resolve_agent_harness(
    *,
    harness: str,
    model: str = "",
    wrapper_namespace: str = "miner.agent_v1.wrappers",
    provider: str = "openrouter",
    max_turns: int = 30,
) -> AgentHarnessProfile:
    normalized = harness.strip().lower().replace("_", "-")
    if not normalized:
        return AgentHarnessProfile(harness="", model=model, runtime="")
    if normalized not in SUPPORTED_HARNESSES:
        raise ValueError(f"Unsupported agent harness: {harness}")
    if normalized in NATIVE_HARNESSES:
        return AgentHarnessProfile(harness=normalized, model=model, runtime=normalized)
    wrapper = f"{shlex.quote(sys.executable)} -m {wrapper_namespace}.{_wrapper_module(normalized)}"
    return AgentHarnessProfile(
        harness=normalized,
        model=model,
        runtime="agent-cli",
        cli_command=wrapper,
        inner_command=_inner_command(normalized, model=model, provider=provider, max_turns=max_turns),
    )


def quote_command(command: str) -> list[str]:
    return shlex.split(command) if command.strip() else []


def _wrapper_module(harness: str) -> str:
    if harness == "hermes-cli":
        return "hermes_prompt"
    if harness == "codex-cli":
        return "codex_prompt"
    if harness == "claude-cli":
        return "claude_prompt"
    raise ValueError(f"No wrapper module for harness: {harness}")


def _inner_command(harness: str, *, model: str, provider: str, max_turns: int) -> str:
    if harness == "hermes-cli":
        model_args = f" -m {shlex.quote(model)}" if model else ""
        return (
            f"{shlex.quote(_cli_executable('hermes'))} chat "
            f"--provider {shlex.quote(provider)}{model_args} --max-turns {int(max_turns)} -q"
        )
    if harness == "codex-cli":
        model_args = f" --model {shlex.quote(model)}" if model else ""
        return f"{shlex.quote(_cli_executable('codex'))} exec{model_args} --json --sandbox workspace-write --skip-git-repo-check"
    if harness == "claude-cli":
        model_args = f" --model {shlex.quote(model)}" if model else ""
        return (
            f"{shlex.quote(_cli_executable('claude'))} -p "
            f"--permission-mode bypassPermissions --output-format text{model_args}"
        )
    return ""


def _cli_executable(name: str) -> str:
    discovered = shutil.which(name)
    if discovered:
        return discovered
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / name,
        home / ".hermes" / "hermes-agent" / name,
        home / ".codex" / "bin" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return name
