"""Explicit open-weight Silver judges for the competition pilot.

These model IDs are a pilot choice. A paid round must freeze and cost its
resolved provider endpoints before intake opens.
"""

from __future__ import annotations

from dataclasses import replace

from validator.agent_v1.adjudication_config import SilverAdjudicationConfig
from validator.agent_v1.adjudication_runner import AdjudicationPass

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


def provisional_judge_configs() -> tuple[SilverAdjudicationConfig, SilverAdjudicationConfig]:
    """Return the named pilot configurations without reading a credential."""
    j1 = SilverAdjudicationConfig(
        mode="openai-compatible", api_base="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model_a=PROVISIONAL_JUDGE_MODELS["J1"][0],
        model_b=PROVISIONAL_JUDGE_MODELS["J1"][1],
        tiebreak_model=PROVISIONAL_TIEBREAK_MODEL,
        temperature=1.0, max_tokens=8192, timeout_seconds=180.0,
        max_in_flight=16,
    )
    return j1, replace(
        j1, model_a=PROVISIONAL_JUDGE_MODELS["J2"][0],
        model_b=PROVISIONAL_JUDGE_MODELS["J2"][1],
    )


def build_provisional_judges(
) -> tuple[dict[str, tuple[AdjudicationPass, AdjudicationPass]], AdjudicationPass]:
    """Build the pilot panels in the trusted controller's credential context."""
    j1, j2 = provisional_judge_configs()
    return build_weekly_judges(
        j1=j1, j2=j2, allowed_models=set(PROVISIONAL_MODEL_ALLOWLIST),
    )
