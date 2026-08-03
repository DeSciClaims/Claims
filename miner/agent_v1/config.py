from __future__ import annotations

import os
import shlex
from pathlib import Path

from pydantic import BaseModel


class AgentV1Config(BaseModel):
    base_dir: Path
    package_dir: Path
    cache_dir: Path
    output_dir: Path
    runtime: str = "dspy-react"
    skill_dir: Path
    timeout_seconds: int = 1800
    max_source_chars: int = 60000
    pdf_reader: str = "pdf-inspector"
    grobid_url: str = "http://localhost:8070/"
    grobid_timeout_s: int = 120
    grobid_retries: int = 3
    grobid_retry_wait_s: int = 2
    model: str = "openrouter/google/gemma-4-27b-it"
    api_key: str | None = None
    api_base: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.2
    max_tokens: int = 32768
    max_agent_iters: int = 4
    max_repair_attempts: int = 3
    cli_command: list[str] = []

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "AgentV1Config":
        resolved_base_dir = base_dir or Path(__file__).resolve().parents[2]
        package_dir = Path(__file__).resolve().parent
        return cls(
            base_dir=resolved_base_dir,
            package_dir=package_dir,
            cache_dir=package_dir / ".cache",
            output_dir=package_dir / "outputs" / "agent_v1",
            runtime=os.getenv("SUBNET_CLAIMS_AGENT_RUNTIME", "dspy-react"),
            skill_dir=Path(os.getenv("SUBNET_CLAIMS_AGENT_SKILL_DIR", str(package_dir / "skills" / "compiler"))),
            timeout_seconds=int(os.getenv("SUBNET_CLAIMS_AGENT_TIMEOUT", "1800")),
            max_source_chars=int(os.getenv("SUBNET_CLAIMS_AGENT_MAX_SOURCE_CHARS", "60000")),
            pdf_reader=_pdf_reader_from_env(),
            grobid_url=os.getenv("GROBID_URL", "http://localhost:8070/"),
            grobid_timeout_s=int(os.getenv("SUBNET_CLAIMS_GROBID_TIMEOUT_S", "120")),
            grobid_retries=int(os.getenv("SUBNET_CLAIMS_GROBID_RETRIES", "3")),
            grobid_retry_wait_s=int(os.getenv("SUBNET_CLAIMS_GROBID_RETRY_WAIT_S", "2")),
            model=os.getenv(
                "SUBNET_CLAIMS_AGENT_MODEL",
                os.getenv("OPENROUTER_MODEL", "openrouter/google/gemma-4-27b-it"),
            ),
            api_key=os.getenv("OPENROUTER_API_KEY"),
            api_base=os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
            temperature=float(os.getenv("SUBNET_CLAIMS_AGENT_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("SUBNET_CLAIMS_AGENT_MAX_TOKENS", "32768")),
            max_agent_iters=int(os.getenv("SUBNET_CLAIMS_AGENT_MAX_ITERS", "4")),
            max_repair_attempts=int(os.getenv("SUBNET_CLAIMS_AGENT_MAX_REPAIR_ATTEMPTS", "3")),
            cli_command=shlex.split(os.getenv("SUBNET_CLAIMS_AGENT_CLI_COMMAND", "")),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise SystemExit("OPENROUTER_API_KEY is required for agent_v1 model runtimes.")
        return self.api_key


def _pdf_reader_from_env() -> str:
    return (
        os.getenv("SUBNET_CLAIMS_PDF_READER")
        or os.getenv("SUBNET_CLAIMS_PDF_EXTRACTION_METHOD")
        or os.getenv("CLAIMS_PDF_READER")
        or "pdf-inspector"
    )
