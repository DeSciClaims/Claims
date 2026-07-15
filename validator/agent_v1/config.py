from __future__ import annotations

import os
import shlex
from pathlib import Path

from pydantic import BaseModel


class AgentV1ValidatorConfig(BaseModel):
    base_dir: Path
    package_dir: Path
    output_dir: Path
    runtime: str = "dspy-react"
    skill_dir: Path
    timeout_seconds: int = 1800
    model: str = "openrouter/openai/gpt-4o-mini"
    api_key: str | None = None
    api_base: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.0
    max_tokens: int = 16384
    max_agent_iters: int = 4
    cli_command: list[str] = []
    skip_rigor_agent: bool = False

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "AgentV1ValidatorConfig":
        resolved_base_dir = base_dir or Path(__file__).resolve().parents[2]
        package_dir = Path(__file__).resolve().parent
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
            api_key=os.getenv("OPENROUTER_API_KEY"),
            api_base=os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
            temperature=float(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_TEMPERATURE", "0.0")),
            max_tokens=int(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_TOKENS", "16384")),
            max_agent_iters=int(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_ITERS", "4")),
            cli_command=shlex.split(os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND", "")),
            skip_rigor_agent=os.getenv("SUBNET_CLAIMS_VALIDATOR_SKIP_RIGOR_AGENT", "").lower() in {"1", "true", "yes"},
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise SystemExit("OPENROUTER_API_KEY is required for validator.agent_v1 model runtimes.")
        return self.api_key
