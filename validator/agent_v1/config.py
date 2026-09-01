from __future__ import annotations

import os
import shlex
from pathlib import Path

from pydantic import BaseModel

from miner.agent_v1.provider import normalize_provider, provider_api_base, provider_api_key_env


class AgentV1ValidatorConfig(BaseModel):
    base_dir: Path
    package_dir: Path
    output_dir: Path
    runtime: str = "dspy-react"
    skill_dir: Path
    timeout_seconds: int = 1800
    model: str = "openrouter/openai/gpt-4o-mini"
    provider: str = "openrouter"
    api_key_env: str = "OPENROUTER_API_KEY"
    api_key: str | None = None
    api_base: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.0
    max_tokens: int = 16384
    max_agent_iters: int = 4
    cli_command: list[str] = []
    skip_rigor_agent: bool = False
    validation_mode: str = "deterministic"

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "AgentV1ValidatorConfig":
        resolved_base_dir = base_dir or Path(__file__).resolve().parents[2]
        package_dir = Path(__file__).resolve().parent
        provider = normalize_provider(
            os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_PROVIDER")
            or os.getenv("CLAIMS_RIGOR_PROVIDER")
            or os.getenv("HERMES_PROVIDER")
        )
        api_key_env = provider_api_key_env(
            provider,
            os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_API_KEY_ENV")
            or os.getenv("CLAIMS_RIGOR_API_KEY_ENV"),
        )
        api_base = provider_api_base(
            provider,
            os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_API_BASE")
            or os.getenv("CLAIMS_RIGOR_API_BASE"),
        )
        return cls(
            base_dir=resolved_base_dir,
            package_dir=package_dir,
            output_dir=package_dir / "outputs",
            runtime=os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_RUNTIME", "dspy-react"),
            skill_dir=Path(
                os.getenv(
                    "SUBNET_CLAIMS_VALIDATOR_AGENT_SKILL_DIR",
                    str(package_dir / "skills" / "rigor_reviewer"),
                )
            ),
            timeout_seconds=int(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_TIMEOUT", "1800")),
            model=os.getenv(
                "SUBNET_CLAIMS_VALIDATOR_AGENT_MODEL",
                os.getenv("SUBNET_CLAIMS_AGENT_MODEL", os.getenv("OPENROUTER_MODEL", "openrouter/openai/gpt-4o-mini")),
            ),
            provider=provider,
            api_key_env=api_key_env,
            api_key=os.getenv(api_key_env),
            api_base=api_base,
            temperature=float(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_TEMPERATURE", "0.0")),
            max_tokens=int(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_TOKENS", "16384")),
            max_agent_iters=int(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_ITERS", "4")),
            cli_command=shlex.split(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND", "")),
            skip_rigor_agent=os.getenv("SUBNET_CLAIMS_VALIDATOR_SKIP_RIGOR_AGENT", "").lower() in {"1", "true", "yes"},
            validation_mode=os.getenv(
                "SUBNET_CLAIMS_VALIDATOR_VALIDATION_MODE",
                os.getenv("CLAIMS_AGENT_V1_VALIDATION_MODE", "deterministic"),
            )
            .strip()
            .lower(),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise SystemExit(f"{self.api_key_env} is required for validator.agent_v1 model runtimes.")
        return self.api_key
