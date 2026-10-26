from dataclasses import replace

import pytest

from validator.agent_v1.adjudication_config import SilverAdjudicationConfig

from competition.decision_router import PROMPT_SHA256
from competition.evaluation_policy import FrozenEvaluationPolicy, judge_runtime_config
from competition.judges import build_weekly_judges
from competition.publication import canonical_sha256
from competition.silver_runtime import silver_runtime_config


def _configs():
    first = SilverAdjudicationConfig(
        mode="openai-compatible", api_base="https://gateway.example/v1",
        api_key_env="COMPETITION_TEST_JUDGE_KEY",
        model_a="org/model-a", model_b="org/model-b", tiebreak_model="org/tie",
    )
    return first, replace(first, model_a="org/model-c", model_b="org/model-d")


def test_builds_two_model_backed_panels_and_hides_credential(monkeypatch):
    monkeypatch.setenv("COMPETITION_TEST_JUDGE_KEY", "private-judge-credential")
    j1, j2 = _configs()
    panels, tie = build_weekly_judges(
        j1=j1, j2=j2,
        allowed_models={"org/model-a", "org/model-b", "org/model-c", "org/model-d", "org/tie"},
    )
    config = {
        "decision_routing": {
            "provider": "openai", "model": "gpt-6-luna",
            "api_base": "https://api.openai.com/v1", "prompt_sha256": PROMPT_SHA256,
            "prescreen_confidence_threshold": 0.8, "screen_confidence_threshold": 0.9,
        },
        "silver_runtime": silver_runtime_config(),
        **judge_runtime_config(panels, tie),
    }
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(config),
        configuration=config, adjudication_passes_by_judge=panels,
        tiebreak_pass=tie, source_context_for_paper=lambda _: {}, decision_api_key="decision-key",
    )
    assert panels["J1"][0].model == "org/model-a"
    assert panels["J2"][0].model == "org/model-c"
    assert tie.model == "org/tie"
    assert "private-judge-credential" not in repr(config)
    assert "private-judge-credential" not in repr(policy)
    assert "private-judge-credential" not in repr(panels)
    assert "private-judge-credential" not in repr(tie)


def test_rejects_non_allowlisted_and_non_model_judges(monkeypatch):
    monkeypatch.setenv("COMPETITION_TEST_JUDGE_KEY", "private-judge-credential")
    j1, j2 = _configs()
    with pytest.raises(ValueError, match="non-allowlisted"):
        build_weekly_judges(j1=j1, j2=j2, allowed_models={"org/model-a"})
    with pytest.raises(ValueError, match="model-backed"):
        build_weekly_judges(j1=replace(j1, mode="static"), j2=j2,
                            allowed_models={"org/model-a", "org/model-b", "org/model-c",
                                            "org/model-d", "org/tie"})


def test_requires_one_shared_tiebreak_configuration(monkeypatch):
    monkeypatch.setenv("COMPETITION_TEST_JUDGE_KEY", "private-judge-credential")
    j1, j2 = _configs()
    allowed = {"org/model-a", "org/model-b", "org/model-c", "org/model-d", "org/tie", "org/other"}
    with pytest.raises(ValueError, match="tiebreak"):
        build_weekly_judges(j1=j1, j2=replace(j2, tiebreak_model="org/other"),
                            allowed_models=allowed)
    with pytest.raises(ValueError, match="credential source"):
        build_weekly_judges(j1=j1, j2=replace(j2, api_key_env="OTHER_KEY"),
                            allowed_models=allowed)
