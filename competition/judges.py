"""Build two explicit, allowlisted full Silver judge configurations."""

from __future__ import annotations

from validator.agent_v1.adjudication_config import (
    SilverAdjudicationConfig, build_silver_adjudication_passes,
)
from validator.agent_v1.adjudication_runner import AdjudicationPass
from validator.agent_v1.model_usage import UsageSink

from .evaluation_policy import judge_runtime_config


def build_weekly_judges(
    *, j1: SilverAdjudicationConfig, j2: SilverAdjudicationConfig,
    allowed_models: set[str], usage_sink: UsageSink | None = None,
) -> tuple[dict[str, tuple[AdjudicationPass, AdjudicationPass]], AdjudicationPass]:
    """Resolve J1/J2 and one common tiebreak without exposing provider keys.

    Both panels use the existing two-pass Silver adjudicator. The explicitly
    supplied model allowlist is the frozen week's open-weight model policy.
    Static/replay passes cannot be used for a paid competition round.
    """
    if not allowed_models or any(not model for model in allowed_models):
        raise ValueError("weekly judge model allowlist is empty")
    configurations = {"J1": j1, "J2": j2}
    for judge_id, config in configurations.items():
        if config.mode.strip().lower().replace("_", "-") not in {"openai-compatible", "model", "dspy"}:
            raise ValueError(f"{judge_id} needs a model-backed Silver configuration")
        models = {config.model_a, config.model_b, config.tiebreak_model}
        if ("" in models or not models <= allowed_models or config.model_a == config.model_b):
            raise ValueError(f"{judge_id} uses a missing, repeated or non-allowlisted model")
    if (j1.model_a, j1.model_b) == (j2.model_a, j2.model_b):
        raise ValueError("J1 and J2 must use different primary model configurations")
    if j1.api_key_env != j2.api_key_env:
        raise ValueError("J1 and J2 must use the same trusted tiebreak credential source")
    panels: dict[str, tuple[AdjudicationPass, AdjudicationPass]] = {}
    tiebreaks: dict[str, AdjudicationPass] = {}
    for judge_id, config in configurations.items():
        passes, tiebreak = build_silver_adjudication_passes(
            config, usage_sink=usage_sink,
        )
        if len(passes) != 2 or tiebreak is None:
            raise ValueError(f"{judge_id} did not build two passes and a tiebreak")
        panels[judge_id] = (passes[0], passes[1])
        tiebreaks[judge_id] = tiebreak
    frozen = judge_runtime_config(panels, tiebreaks["J1"])
    other = judge_runtime_config(panels, tiebreaks["J2"])
    if frozen["tiebreak_pass"] != other["tiebreak_pass"]:
        raise ValueError("J1 and J2 must share one frozen tiebreak configuration")
    return panels, tiebreaks["J1"]
