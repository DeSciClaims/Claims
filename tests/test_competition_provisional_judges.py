from competition.evaluation_policy import judge_runtime_config
from competition.provisional_judges import (
    PROVISIONAL_JUDGE_MODELS, PROVISIONAL_MODEL_ALLOWLIST,
    PROVISIONAL_TIEBREAK_MODEL, build_provisional_judges,
    provisional_judge_configs,
)


def test_pilot_models_are_distinct_and_frozen_without_credential(monkeypatch):
    j1, j2 = provisional_judge_configs()
    assert set(PROVISIONAL_JUDGE_MODELS) == {"J1", "J2"}
    assert len(PROVISIONAL_MODEL_ALLOWLIST) == 5
    assert (j1.model_a, j1.model_b) == PROVISIONAL_JUDGE_MODELS["J1"]
    assert (j2.model_a, j2.model_b) == PROVISIONAL_JUDGE_MODELS["J2"]
    assert j1.tiebreak_model == j2.tiebreak_model == PROVISIONAL_TIEBREAK_MODEL
    assert j1.api_base == j2.api_base == "https://openrouter.ai/api/v1"
    assert j1.temperature == j2.temperature == 1.0
    assert j1.max_tokens == j2.max_tokens == 8192

    monkeypatch.setenv("OPENROUTER_API_KEY", "pilot-secret")
    panels, tie = build_provisional_judges()
    frozen = judge_runtime_config(panels, tie)
    assert [row["model"] for row in frozen["judge_panels"]["J1"]] == list(
        PROVISIONAL_JUDGE_MODELS["J1"]
    )
    assert [row["model"] for row in frozen["judge_panels"]["J2"]] == list(
        PROVISIONAL_JUDGE_MODELS["J2"]
    )
    assert frozen["tiebreak_pass"]["model"] == PROVISIONAL_TIEBREAK_MODEL
    assert "pilot-secret" not in repr(frozen)
