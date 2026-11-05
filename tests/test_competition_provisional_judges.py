from competition.evaluation_policy import judge_runtime_config
import pytest
from types import SimpleNamespace
from competition.controller_inputs import build_provisional_evaluation_policy
from competition.provisional_judges import (
    PROVISIONAL_JUDGE_MODELS, PROVISIONAL_MODEL_ALLOWLIST,
    PROVISIONAL_TIEBREAK_MODEL, build_provisional_judges,
    provisional_judge_configs,
    require_provisional_provider_pins,
)


def test_explicit_single_model_rehearsal_is_testnet_only(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    descriptor = {"model": "deepseek/deepseek-v4-flash-0731", "provider_slug": "deepinfra/fp8"}
    specs = {"J1": [descriptor, descriptor], "J2": [descriptor, descriptor],
             "tiebreak": descriptor, "api_base": "https://openrouter.ai/api/v1",
             "max_tokens": 32768, "timeout_seconds": 120, "max_in_flight": 4,
             "temperature": 0.0, "single_model_rehearsal": True}
    panels, tie = build_provisional_judges(specs=specs)
    configuration = {**judge_runtime_config(panels, tie), "judge_specs": specs}
    require_provisional_provider_pins(configuration, network="testnet")
    policy = build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(round_id="testnet-rehearsal", source_context_for_paper=lambda _: {}),
        decision_api_key="test-decision", weekly_budget_usd=10, judge_specs=specs,
        worker_policy={"allowed_models": [descriptor["model"]], "published_token_ceiling": 800000,
                       "max_inference_output_tokens": 8192, "lease_seconds": 3600,
                       "dollar_limit_usd": "0.10", "inference_base_url": "https://gateway.example/v1"},
    )
    assert policy.configuration["judge_specs"]["single_model_rehearsal"] is True
    with pytest.raises(ValueError, match="restricted to testnet"):
        require_provisional_provider_pins(configuration, network="mainnet")
    with pytest.raises(ValueError, match="repeated"):
        build_provisional_judges(specs={k: v for k, v in specs.items() if k != "single_model_rehearsal"})


def test_pilot_models_are_distinct_and_frozen_without_credential(monkeypatch):
    j1, j2 = provisional_judge_configs()
    assert set(PROVISIONAL_JUDGE_MODELS) == {"J1", "J2"}
    assert len(PROVISIONAL_MODEL_ALLOWLIST) == 5
    assert (j1.model_a, j1.model_b) == PROVISIONAL_JUDGE_MODELS["J1"]
    assert (j2.model_a, j2.model_b) == PROVISIONAL_JUDGE_MODELS["J2"]
    assert j1.tiebreak_model == j2.tiebreak_model == PROVISIONAL_TIEBREAK_MODEL
    assert j1.api_base == j2.api_base == "https://openrouter.ai/api/v1"
    assert j1.temperature == j2.temperature == 0.0
    assert j1.max_tokens == j2.max_tokens == 32768

    monkeypatch.setenv("OPENROUTER_API_KEY", "pilot-secret")
    usage_events = []
    sink = usage_events.append
    panels, tie = build_provisional_judges(usage_sink=sink)
    frozen = judge_runtime_config(panels, tie)
    assert [row["model"] for row in frozen["judge_panels"]["J1"]] == list(
        PROVISIONAL_JUDGE_MODELS["J1"]
    )
    assert [row["model"] for row in frozen["judge_panels"]["J2"]] == list(
        PROVISIONAL_JUDGE_MODELS["J2"]
    )
    assert frozen["tiebreak_pass"]["model"] == PROVISIONAL_TIEBREAK_MODEL
    assert all(adjudication_pass.usage_sink is sink
               for passes in panels.values() for adjudication_pass in passes)
    assert tie.usage_sink is sink
    assert "pilot-secret" not in repr(frozen)
