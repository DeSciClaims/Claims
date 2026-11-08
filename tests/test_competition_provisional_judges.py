from competition.evaluation_policy import judge_runtime_config
import pytest
from types import SimpleNamespace
from competition.controller_inputs import build_provisional_evaluation_policy
from competition.provisional_judges import (
    PROVISIONAL_JUDGE_MODELS, PROVISIONAL_MODEL_ALLOWLIST,
    PROVISIONAL_TIEBREAK_MODEL, build_provisional_judges,
    provisional_judge_configs,
    require_provisional_provider_pins,
    validate_provisional_judge_routes,
)


def _repeated_model_specs(**flags):
    descriptor = {"model": "org/open-weight-model"}
    return {
        "J1": [descriptor, descriptor], "J2": [descriptor, descriptor],
        "tiebreak": descriptor, "api_base": "https://openrouter.ai/api/v1",
        "max_tokens": 32768, "timeout_seconds": 120, "max_in_flight": 4,
        "temperature": 0.0, **flags,
    }


@pytest.mark.parametrize("workers", [1, 50, 64, 200])
def test_judge_concurrency_uses_configured_positive_limit(workers):
    specs = {**_repeated_model_specs(allow_repeated_models=True), "max_in_flight": workers}
    j1, j2 = provisional_judge_configs(specs)
    assert j1.max_in_flight == workers
    assert j2.max_in_flight == workers


@pytest.mark.parametrize("workers", [True, False, 0, -1, 1.0, "50", None])
def test_judge_concurrency_rejects_invalid_limits(workers):
    specs = {**_repeated_model_specs(allow_repeated_models=True), "max_in_flight": workers}
    with pytest.raises(ValueError, match="inference settings"):
        provisional_judge_configs(specs)


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_explicit_repeated_model_policy_is_frozen_for_each_network(network, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    specs = _repeated_model_specs(allow_repeated_models=True)
    panels, tie = build_provisional_judges(specs=specs)
    configuration = {**judge_runtime_config(panels, tie), "judge_specs": specs}
    validate_provisional_judge_routes(configuration, network=network)
    policy = build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(round_id=network + "-round", source_context_for_paper=lambda _: {}),
        decision_api_key="test-decision", weekly_budget_usd=10, judge_specs=specs,
        worker_policy={"allowed_models": ["org/open-weight-model"], "published_token_ceiling": 800000,
                       "max_inference_output_tokens": 8192, "lease_seconds": 3600,
                       "dollar_limit_usd": "0.10", "inference_base_url": "https://gateway.example/v1"},
    )
    assert policy.configuration["judge_specs"] == specs
    assert "single_model_rehearsal" not in policy.configuration["judge_specs"]
    assert all(row["model"] == "org/open-weight-model"
               for panel in configuration["judge_panels"].values() for row in panel)
    assert configuration["tiebreak_pass"]["model"] == "org/open-weight-model"
    policy._verify_frozen_runtime()


@pytest.mark.parametrize("flag", ["allow_repeated_models", "single_model_rehearsal"])
@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_model_diversity_flags_require_actual_booleans(flag, value):
    specs = _repeated_model_specs(**{flag: value})
    with pytest.raises(ValueError, match="boolean"):
        provisional_judge_configs(specs)
    with pytest.raises(ValueError, match="boolean"):
        validate_provisional_judge_routes({"judge_specs": specs}, network="testnet")


@pytest.mark.parametrize("generic,legacy", [(True, True), (True, False), (False, True), (False, False)])
def test_model_diversity_flags_cannot_be_combined(generic, legacy):
    specs = _repeated_model_specs(allow_repeated_models=generic, single_model_rehearsal=legacy)
    with pytest.raises(ValueError, match="cannot be combined"):
        provisional_judge_configs(specs)
    with pytest.raises(ValueError, match="cannot be combined"):
        validate_provisional_judge_routes({"judge_specs": specs}, network="mainnet")


@pytest.mark.parametrize("flags", [{}, {"allow_repeated_models": False}, {"single_model_rehearsal": False}])
def test_repeated_models_still_require_explicit_permission(flags):
    with pytest.raises(ValueError, match="repeated"):
        build_provisional_judges(specs=_repeated_model_specs(**flags))


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


@pytest.mark.parametrize("slug", [None, ""])
def test_model_only_and_explicit_empty_routes_are_frozen_as_automatic(slug, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture")
    descriptor = {"model": "deepseek/deepseek-v4-flash-0731"}
    if slug is not None:
        descriptor["provider_slug"] = slug
    specs = {"J1": [descriptor, descriptor], "J2": [descriptor, descriptor],
             "tiebreak": descriptor, "api_base": "https://openrouter.ai/api/v1",
             "temperature": 0, "max_tokens": 32768, "timeout_seconds": 120,
             "max_in_flight": 4, "single_model_rehearsal": True}
    panels, tie = build_provisional_judges(specs=specs)
    configuration = {**judge_runtime_config(panels, tie), "judge_specs": specs}
    validate_provisional_judge_routes(configuration, network="testnet")
    require_provisional_provider_pins(configuration, network="testnet")
    assert all(pass_["provider_slug"] == "" for panel in configuration["judge_panels"].values() for pass_ in panel)
    assert configuration["tiebreak_pass"]["provider_slug"] == ""


@pytest.mark.parametrize("descriptor", [{"model": "model", "provider_slug": None},
    {"model": "model", "provider_slug": 5}, {"model": "model", "provider_slug": "bad slug"},
    {"model": "model", "unexpected": True}, {"model": ""}, {"model": 4}])
def test_optional_provider_routes_do_not_accept_invalid_or_unknown_fields(descriptor):
    specs = {"J1": [descriptor, descriptor], "J2": [descriptor, descriptor],
             "tiebreak": descriptor, "api_base": "https://openrouter.ai/api/v1",
             "temperature": 0, "max_tokens": 32768, "timeout_seconds": 120,
             "max_in_flight": 4, "single_model_rehearsal": True}
    with pytest.raises(ValueError, match="invalid"):
        provisional_judge_configs(specs)
