"""Explicit open-weight Silver judges for the competition pilot.

These model IDs are a pilot choice. A paid round must freeze and cost its
resolved provider endpoints before intake opens.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any, Mapping

from validator.agent_v1.adjudication_config import SilverAdjudicationConfig
from validator.agent_v1.adjudication_runner import AdjudicationPass
from validator.agent_v1.model_usage import UsageSink

from .judges import build_weekly_judges


PROVISIONAL_JUDGE_MODELS = {
    "J1": ("qwen/qwen3-32b", "openai/gpt-oss-120b"),
    "J2": ("deepseek/deepseek-v3.2", "meta-llama/llama-4-maverick"),
}
PROVISIONAL_TIEBREAK_MODEL = "qwen/qwen3-235b-a22b-2507"
PROVISIONAL_MODEL_ALLOWLIST = frozenset({
    *PROVISIONAL_JUDGE_MODELS["J1"], *PROVISIONAL_JUDGE_MODELS["J2"],
    PROVISIONAL_TIEBREAK_MODEL,
})


def require_provisional_provider_pins(configuration: Mapping[str, Any]) -> None:
    """Reject a paid pilot whose OpenRouter endpoint could silently change."""
    panels = configuration.get("judge_panels")
    tie = configuration.get("tiebreak_pass")
    if (not isinstance(panels, dict) or set(panels) != {"J1", "J2"}
        or any(not isinstance(panels[judge], list) or len(panels[judge]) != 2
               for judge in ("J1", "J2"))
        or not isinstance(tie, dict)):
        raise ValueError("pilot judge provider configuration is incomplete")
    descriptors = [*panels["J1"], *panels["J2"], tie]
    if any(not isinstance(descriptor, dict)
           or descriptor.get("api_base") != "https://openrouter.ai/api/v1"
           or not isinstance(descriptor.get("provider_slug"), str)
           or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}",
                               descriptor["provider_slug"])
           for descriptor in descriptors):
        raise ValueError("pilot judges need pinned OpenRouter providers before intake")


def provisional_judge_configs(
    specs: Mapping[str, Any] | None = None,
) -> tuple[SilverAdjudicationConfig, SilverAdjudicationConfig]:
    """Return the named pilot configurations without reading a credential."""
    if specs is not None:
        if (not isinstance(specs, dict) or set(specs) != {
            "J1", "J2", "tiebreak", "api_base", "temperature",
            "max_tokens", "timeout_seconds", "max_in_flight",
        } or specs["api_base"] != "https://openrouter.ai/api/v1"
            or any(not isinstance(specs.get(panel), list) or len(specs[panel]) != 2
                   for panel in ("J1", "J2"))):
            raise ValueError("frozen judge specifications are invalid")
        descriptors = [*specs["J1"], *specs["J2"], specs["tiebreak"]]
        if any(not isinstance(item, dict) or set(item) != {"model", "provider_slug"}
               or not isinstance(item["model"], str) or not item["model"]
               or not isinstance(item["provider_slug"], str)
               or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}", item["provider_slug"])
               for item in descriptors):
            raise ValueError("frozen judge model or provider is invalid")
        if (isinstance(specs["temperature"], bool)
            or not isinstance(specs["temperature"], (int, float))
            or not 0 <= specs["temperature"] <= 2
            or type(specs["max_tokens"]) is not int
            or not 1 <= specs["max_tokens"] <= 32768
            or isinstance(specs["timeout_seconds"], bool)
            or not isinstance(specs["timeout_seconds"], (int, float))
            or not 1 <= specs["timeout_seconds"] <= 600
            or type(specs["max_in_flight"]) is not int
            or not 1 <= specs["max_in_flight"] <= 64):
            raise ValueError("frozen judge inference settings are invalid")
        j1 = SilverAdjudicationConfig(
            mode="openai-compatible", api_base=specs["api_base"],
            api_key_env="OPENROUTER_API_KEY",
            model_a=specs["J1"][0]["model"],
            model_b=specs["J1"][1]["model"],
            tiebreak_model=specs["tiebreak"]["model"],
            provider_slug_a=specs["J1"][0]["provider_slug"],
            provider_slug_b=specs["J1"][1]["provider_slug"],
            provider_slug_tiebreak=specs["tiebreak"]["provider_slug"],
            temperature=float(specs["temperature"]),
            max_tokens=specs["max_tokens"],
            timeout_seconds=float(specs["timeout_seconds"]),
            max_in_flight=specs["max_in_flight"],
        )
        return j1, replace(
            j1, model_a=specs["J2"][0]["model"],
            model_b=specs["J2"][1]["model"],
            provider_slug_a=specs["J2"][0]["provider_slug"],
            provider_slug_b=specs["J2"][1]["provider_slug"],
        )
    j1 = SilverAdjudicationConfig(
        mode="openai-compatible", api_base="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model_a=PROVISIONAL_JUDGE_MODELS["J1"][0],
        model_b=PROVISIONAL_JUDGE_MODELS["J1"][1],
        tiebreak_model=PROVISIONAL_TIEBREAK_MODEL,
        provider_slug_a=os.getenv("CLAIMS_JUDGE_J1_A_PROVIDER", ""),
        provider_slug_b=os.getenv("CLAIMS_JUDGE_J1_B_PROVIDER", ""),
        provider_slug_tiebreak=os.getenv("CLAIMS_JUDGE_TIEBREAK_PROVIDER", ""),
        temperature=1.0, max_tokens=8192, timeout_seconds=180.0,
        max_in_flight=16,
    )
    return j1, replace(
        j1, model_a=PROVISIONAL_JUDGE_MODELS["J2"][0],
        model_b=PROVISIONAL_JUDGE_MODELS["J2"][1],
        provider_slug_a=os.getenv("CLAIMS_JUDGE_J2_A_PROVIDER", ""),
        provider_slug_b=os.getenv("CLAIMS_JUDGE_J2_B_PROVIDER", ""),
    )


def build_provisional_judges(
    *, usage_sink: UsageSink | None = None,
    specs: Mapping[str, Any] | None = None,
) -> tuple[dict[str, tuple[AdjudicationPass, AdjudicationPass]], AdjudicationPass]:
    """Build the pilot panels in the trusted controller's credential context."""
    j1, j2 = provisional_judge_configs(specs)
    allowed = set(PROVISIONAL_MODEL_ALLOWLIST)
    if specs is not None:
        allowed = {specs["tiebreak"]["model"]}
        for panel in ("J1", "J2"):
            allowed.update(item["model"] for item in specs[panel])
    return build_weekly_judges(
        j1=j1, j2=j2, allowed_models=allowed,
        usage_sink=usage_sink,
    )
