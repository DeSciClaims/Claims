from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from typing import Literal

from .adjudication_passes import CLIAdjudicationPass, OpenAICompatibleAdjudicationPass, StaticAdjudicationPass
from .adjudication_runner import AdjudicationPass


SilverAdjudicationMode = Literal["static", "local-replay", "openai-compatible", "model", "cli", "hermes-cli", "codex-cli", "claude-cli"]


@dataclass(frozen=True)
class SilverAdjudicationConfig:
    mode: str = "static"
    static_disposition: str = "benign_difference"
    local_replay_disposition: str = "reference_error"
    api_base: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model_a: str = "gpt-5"
    model_b: str = "gpt-5-mini"
    tiebreak_model: str = ""
    cli_command_a: str = ""
    cli_command_b: str = ""
    cli_tiebreak_command: str = ""
    cli_command_template: str = ""
    cli_prompt_mode: str = "append"
    cli_timeout_seconds: float = 900.0
    cli_provider: str = "openrouter"
    cli_max_turns: int = 10

    @classmethod
    def from_env(cls, *, mode_default: str = "static", api_key_env_default: str = "OPENAI_API_KEY") -> "SilverAdjudicationConfig":
        return cls(
            mode=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODE", mode_default),
            static_disposition=os.getenv("CLAIMS_SILVER_STATIC_DISPOSITION", "benign_difference"),
            local_replay_disposition=os.getenv("CLAIMS_SILVER_LOCAL_REPLAY_DISPOSITION", "reference_error"),
            api_base=os.getenv("CLAIMS_SILVER_ADJUDICATION_API_BASE", os.getenv("OPENROUTER_API_BASE", "https://api.openai.com/v1")),
            api_key_env=os.getenv("CLAIMS_SILVER_ADJUDICATION_API_KEY_ENV", api_key_env_default),
            model_a=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", "gpt-5"),
            model_b=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_B", "gpt-5-mini"),
            tiebreak_model=os.getenv("CLAIMS_SILVER_ADJUDICATION_TIEBREAK_MODEL", ""),
            cli_command_a=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_A", ""),
            cli_command_b=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_B", ""),
            cli_tiebreak_command=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_TIEBREAK_COMMAND", ""),
            cli_command_template=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_TEMPLATE", ""),
            cli_prompt_mode=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_PROMPT_MODE", "append"),
            cli_timeout_seconds=float(os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_TIMEOUT", "900")),
            cli_provider=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_PROVIDER", "openrouter"),
            cli_max_turns=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_MAX_TURNS", "10")),
        )


def build_silver_adjudication_passes(config: SilverAdjudicationConfig) -> tuple[list[AdjudicationPass], AdjudicationPass | None]:
    mode = _normalize_mode(config.mode)
    if mode == "static":
        return _static_passes(config.static_disposition)
    if mode == "local-replay":
        return _local_replay_passes(config.local_replay_disposition)
    if mode == "openai-compatible":
        return _openai_compatible_passes(config)
    if mode in {"cli", "hermes-cli", "codex-cli", "claude-cli"}:
        return _cli_passes(config, mode=mode)
    raise ValueError(f"Unknown Silver adjudication mode: {config.mode}")


def _normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower().replace("_", "-")
    if normalized == "model":
        return "openai-compatible"
    return normalized


def _static_passes(disposition: str) -> tuple[list[AdjudicationPass], None]:
    return (
        [
            StaticAdjudicationPass("pass_a", "static_a", "static", {}, default_disposition=disposition),  # type: ignore[arg-type]
            StaticAdjudicationPass("pass_b", "static_b", "static", {}, default_disposition=disposition),  # type: ignore[arg-type]
        ],
        None,
    )


def _local_replay_passes(disposition: str) -> tuple[list[AdjudicationPass], None]:
    def completion(messages: list[dict[str, str]]) -> str:
        cited_span_ids = _span_ids_from_messages(messages)
        return json.dumps(
            {
                "disposition": disposition,
                "material_findings": ["local_e2e_consensus", disposition],
                "cited_span_ids": cited_span_ids[:3],
                "confidence": 0.91,
                "rationale": "Local E2E replay pass accepts the configured adjudication disposition.",
                "insufficient_information": False,
            }
        )

    return (
        [
            OpenAICompatibleAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="silver-local-e2e-a",
                model_runtime_id="openai-compatible-replay-a",
                model="local-replay-a",
                api_key="local",
                completion_fn=completion,
            ),
            OpenAICompatibleAdjudicationPass(
                pass_id="pass_b",
                adjudication_profile_id="silver-local-e2e-b",
                model_runtime_id="openai-compatible-replay-b",
                model="local-replay-b",
                api_key="local",
                completion_fn=completion,
            ),
        ],
        None,
    )


def _openai_compatible_passes(config: SilverAdjudicationConfig) -> tuple[list[AdjudicationPass], AdjudicationPass | None]:
    api_key = os.getenv(config.api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{config.api_key_env} is required for Silver model adjudication.")
    model_a = _direct_api_model_id(config.model_a, config.api_base)
    model_b = _direct_api_model_id(config.model_b, config.api_base)
    passes: list[AdjudicationPass] = [
        OpenAICompatibleAdjudicationPass(
            pass_id="pass_a",
            adjudication_profile_id=f"openai_compatible:{model_a}",
            model_runtime_id="openai-compatible-chat-completions",
            model=model_a,
            api_key=api_key,
            api_base=config.api_base,
        ),
        OpenAICompatibleAdjudicationPass(
            pass_id="pass_b",
            adjudication_profile_id=f"openai_compatible:{model_b}",
            model_runtime_id="openai-compatible-chat-completions",
            model=model_b,
            api_key=api_key,
            api_base=config.api_base,
        ),
    ]
    tiebreak = None
    if config.tiebreak_model.strip():
        tiebreak_model = _direct_api_model_id(config.tiebreak_model, config.api_base)
        tiebreak = OpenAICompatibleAdjudicationPass(
            pass_id="pass_c",
            adjudication_profile_id=f"openai_compatible:{tiebreak_model}",
            model_runtime_id="openai-compatible-chat-completions",
            model=tiebreak_model,
            api_key=api_key,
            api_base=config.api_base,
        )
    return passes, tiebreak


def _cli_passes(config: SilverAdjudicationConfig, *, mode: str) -> tuple[list[AdjudicationPass], AdjudicationPass | None]:
    command_a = _cli_command(config, explicit=config.cli_command_a, model=config.model_a, mode=mode)
    command_b = _cli_command(config, explicit=config.cli_command_b, model=config.model_b, mode=mode)
    passes: list[AdjudicationPass] = [
        CLIAdjudicationPass(
            pass_id="pass_a",
            adjudication_profile_id=f"{mode}:{config.model_a}",
            model_runtime_id=mode,
            command=command_a,
            prompt_mode=config.cli_prompt_mode,
            timeout_seconds=config.cli_timeout_seconds,
        ),
        CLIAdjudicationPass(
            pass_id="pass_b",
            adjudication_profile_id=f"{mode}:{config.model_b}",
            model_runtime_id=mode,
            command=command_b,
            prompt_mode=config.cli_prompt_mode,
            timeout_seconds=config.cli_timeout_seconds,
        ),
    ]
    tiebreak = None
    if config.tiebreak_model.strip():
        tiebreak = CLIAdjudicationPass(
            pass_id="pass_c",
            adjudication_profile_id=f"{mode}:{config.tiebreak_model}",
            model_runtime_id=mode,
            command=_cli_command(config, explicit=config.cli_tiebreak_command, model=config.tiebreak_model, mode=mode),
            prompt_mode=config.cli_prompt_mode,
            timeout_seconds=config.cli_timeout_seconds,
        )
    return passes, tiebreak


def _cli_command(config: SilverAdjudicationConfig, *, explicit: str, model: str, mode: str) -> list[str]:
    command = explicit.strip() or config.cli_command_template.strip()
    if command:
        return shlex.split(command.format(model=model))
    if mode == "hermes-cli":
        hermes = os.getenv("HERMES_CMD", os.getenv("HERMES", "hermes"))
        return shlex.split(f"{hermes} chat --provider {config.cli_provider} -m {model} --max-turns {config.cli_max_turns} -q")
    if mode == "codex-cli":
        codex = os.getenv("CODEX_CMD", "codex")
        model_args = f" --model {shlex.quote(model)}" if model.strip() else ""
        return shlex.split(f"{codex} exec{model_args} --json --sandbox workspace-write --skip-git-repo-check")
    if mode == "claude-cli":
        claude = os.getenv("CLAUDE_CMD", "claude")
        model_args = f" --model {shlex.quote(model)}" if model.strip() else ""
        return shlex.split(f"{claude} -p --permission-mode bypassPermissions --output-format text{model_args}")
    raise ValueError("CLI adjudication requires CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_TEMPLATE or per-pass commands.")


def _direct_api_model_id(model: str, api_base: str) -> str:
    if "openrouter.ai/api" in api_base and model.startswith("openrouter/"):
        return model.removeprefix("openrouter/")
    return model


def _span_ids_from_messages(messages: list[dict[str, str]]) -> list[str]:
    text = "\n".join(message.get("content", "") for message in messages)
    return sorted(set(item for item in _candidate_span_tokens(text) if item))


def _candidate_span_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for marker in ('"source_span_ids":', '"cited_span_ids":'):
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            bracket_start = text.find("[", index)
            bracket_end = text.find("]", bracket_start)
            if bracket_start < 0 or bracket_end < 0:
                break
            try:
                values = json.loads(text[bracket_start : bracket_end + 1])
            except Exception:
                values = []
            tokens.extend(str(value) for value in values if str(value).strip())
            start = bracket_end + 1
    return tokens
