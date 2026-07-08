from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from .versioning import normalize_run_label, versioned_name


DEFAULT_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


class SectionContextV1Config(BaseModel):
    base_dir: Path
    package_dir: Path
    cache_dir: Path
    output_dir: Path
    grobid_url: str = "http://localhost:8070/"
    grobid_timeout_s: int = 120
    grobid_retries: int = 3
    grobid_retry_wait_s: int = 2
    openrouter_api_key: str | None = None
    openrouter_model: str = "openrouter/google/gemma-4-27b-it"
    openrouter_api_base: str = DEFAULT_OPENROUTER_API_BASE
    run_label: str = "default"
    max_section_chars: int = 18000
    max_paper_summary_chars: int = 8000
    max_section_summary_chars: int = 2500
    fallback_section_max_chars: int = 4000
    fallback_section_max_spans: int = 4
    dspy_temperature: float = 1.0
    dspy_max_tokens: int = 32768
    artifact_search_roots: list[Path] = Field(default_factory=list)

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "SectionContextV1Config":
        resolved_base_dir = base_dir or Path(__file__).resolve().parents[2]
        package_dir = Path(__file__).resolve().parent
        run_label = normalize_run_label(os.getenv("SUBNET_CLAIMS_RUN_LABEL"))
        output_name = versioned_name("section_context_v1", run_label)
        return cls(
            base_dir=resolved_base_dir,
            package_dir=package_dir,
            cache_dir=package_dir / ".cache",
            output_dir=package_dir / "outputs" / output_name,
            grobid_url=os.getenv("GROBID_URL", "http://localhost:8070/"),
            grobid_timeout_s=int(os.getenv("SUBNET_CLAIMS_GROBID_TIMEOUT_S", "120")),
            grobid_retries=int(os.getenv("SUBNET_CLAIMS_GROBID_RETRIES", "3")),
            grobid_retry_wait_s=int(os.getenv("SUBNET_CLAIMS_GROBID_RETRY_WAIT_S", "2")),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            openrouter_model=os.getenv(
                "SUBNET_CLAIMS_OPENROUTER_MODEL",
                os.getenv("OPENROUTER_MODEL", "openrouter/google/gemma-4-27b-it"),
            ),
            openrouter_api_base=os.getenv("OPENROUTER_API_BASE", DEFAULT_OPENROUTER_API_BASE),
            run_label=run_label,
            fallback_section_max_chars=int(os.getenv("SUBNET_CLAIMS_FALLBACK_SECTION_MAX_CHARS", "4000")),
            fallback_section_max_spans=int(os.getenv("SUBNET_CLAIMS_FALLBACK_SECTION_MAX_SPANS", "4")),
            artifact_search_roots=[package_dir / "outputs"],
        )

    def require_api_key(self) -> str:
        if not self.openrouter_api_key:
            raise SystemExit("OPENROUTER_API_KEY is required for section_context_v1.")
        return self.openrouter_api_key

    def create_runtime(self) -> "SectionContextV1DSPyRuntime":
        from .dspy_runtime import SectionContextV1DSPyRuntime

        return SectionContextV1DSPyRuntime(config=self)
