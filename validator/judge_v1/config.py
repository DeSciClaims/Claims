from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from .versioning import normalize_run_label, versioned_name


DEFAULT_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


class JudgeV1Config(BaseModel):
    base_dir: Path
    package_dir: Path
    output_dir: Path
    openrouter_api_key: str | None = None
    openrouter_model: str = "openrouter/google/gemma-4-27b-it"
    openrouter_api_base: str = DEFAULT_OPENROUTER_API_BASE
    run_label: str = "default"
    dspy_temperature: float = 0.2
    dspy_max_tokens: int = 32768

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "JudgeV1Config":
        resolved_base_dir = base_dir or Path(__file__).resolve().parents[2]
        package_dir = Path(__file__).resolve().parent
        run_label = normalize_run_label(os.getenv("SUBNET_CLAIMS_RUN_LABEL"))
        output_name = versioned_name("judge_v1", run_label)
        return cls(
            base_dir=resolved_base_dir,
            package_dir=package_dir,
            output_dir=package_dir / "outputs" / output_name,
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            openrouter_model=os.getenv(
                "SUBNET_CLAIMS_OPENROUTER_MODEL",
                os.getenv("OPENROUTER_MODEL", "openrouter/google/gemma-4-27b-it"),
            ),
            openrouter_api_base=os.getenv("OPENROUTER_API_BASE", DEFAULT_OPENROUTER_API_BASE),
            run_label=run_label,
            dspy_temperature=float(os.getenv("SUBNET_CLAIMS_JUDGE_TEMPERATURE", "0.2")),
            dspy_max_tokens=int(os.getenv("SUBNET_CLAIMS_JUDGE_MAX_TOKENS", "32768")),
        )

    def require_api_key(self) -> str:
        if not self.openrouter_api_key:
            raise SystemExit("OPENROUTER_API_KEY is required for judge_v1.")
        return self.openrouter_api_key

    def create_runtime(self) -> "JudgeV1DSPyRuntime":
        from .dspy_runtime import JudgeV1DSPyRuntime

        return JudgeV1DSPyRuntime(config=self)
