from __future__ import annotations

import os


CHUTES_API_BASE = "https://llm.chutes.ai/v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


def normalize_provider(provider: str | None, *, api_base: str = "") -> str:
    normalized = str(provider or "").strip().lower()
    if normalized:
        return normalized
    if "chutes.ai" in api_base:
        return "chutes"
    if "openrouter.ai" in api_base:
        return "openrouter"
    return "openrouter"


def provider_api_base(provider: str, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    if normalize_provider(provider) == "chutes":
        return os.getenv("CHUTES_API_BASE", CHUTES_API_BASE).strip() or CHUTES_API_BASE
    return os.getenv("OPENROUTER_API_BASE", OPENROUTER_API_BASE).strip() or OPENROUTER_API_BASE


def provider_api_key_env(provider: str, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return "CHUTES_API_KEY" if normalize_provider(provider) == "chutes" else "OPENROUTER_API_KEY"


def dspy_model_id(model: str, *, provider: str | None = None, api_base: str = "") -> str:
    normalized = model.strip()
    resolved_provider = normalize_provider(provider, api_base=api_base)
    if resolved_provider == "chutes":
        # DSPy routes custom OpenAI-compatible endpoints through LiteLLM's
        # openai provider. Chutes catalog IDs remain unchanged after the prefix.
        if normalized.startswith("openai/") and normalized.count("/") >= 2:
            return normalized
        return f"openai/{normalized}"
    return normalized
