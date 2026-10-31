import pytest
from pathlib import Path
from validator.agent_v1.adjudication_passes import StaticAdjudicationPass

from competition.evaluation_policy import FrozenEvaluationPolicy, judge_runtime_config
from validator.agent_v1.eligibility_decisions import PROMPT_SHA256
from competition.publication import canonical_sha256
from competition.policy_runtime import policy_runtime_config
from competition.silver_runtime import silver_runtime_config
from competition.validator_workflow import validated_workflow_spec


ROUTING = {
    "schema_version": "claims_competition_eligibility_routing_v2",
    "routing_scope": "paper", "predicate_threshold": 0.5,
    "provider": "openai", "model": "gpt-6-luna", "api_base": "https://api.openai.com/v1",
    "prompt_sha256": PROMPT_SHA256,
    "prescreen_acceptance_fraction": 0.8, "screen_confidence_threshold": 0.9,
}
PANELS = {
    judge_id: tuple(StaticAdjudicationPass(
        f"{judge_id}-{index}", f"profile-{judge_id}", f"model-{judge_id}", {},
    ) for index in (1, 2))
    for judge_id in ("J1", "J2")
}
TIEBREAK = StaticAdjudicationPass(
    "tie", "profile", "model", {}, default_disposition="accepted_improvement",
)


def _configuration(tiebreak=None):
    return {"decision_routing": ROUTING, "silver_runtime": silver_runtime_config(),
            "workflow_specs": validated_workflow_spec(None),
            "competition_policy_runtime": policy_runtime_config(),
            **judge_runtime_config(PANELS, tiebreak)}


CONFIGURATION = _configuration()


def test_stage_factory_freezes_routing_and_never_uses_decisions_in_final(monkeypatch):
    calls = []
    monkeypatch.setattr("competition.evaluation_policy.evaluate_weekly_paper",
                        lambda **kwargs: calls.append(kwargs) or "evaluated")
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION,
        adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda paper_id: {"span-1": f"Source for {paper_id}"},
        decision_api_key="private-key",
    )
    assert "private-key" not in repr(policy)
    assert policy.public_routing_config() == ROUTING
    for stage, expected_threshold in (("prescreen", 0.8), ("screen", 0.9),
                                      ("final", None), ("week_one", None)):
        evaluate = policy.evaluate_for_stage(stage)
        assert evaluate(stage=stage, paper_id="paper-1",
                        judge_id="routing" if stage == "prescreen" else "J1") == "evaluated"
        captured = calls[-1]
        assert captured["source_context_by_span_id"] == {"span-1": "Source for paper-1"}
        assert captured["silver_record_id"].startswith("competition_silver_")
        if expected_threshold is None:
            assert captured["file_agent_workflow"].eligibility_decision_policy is None
        else:
            decision = captured["file_agent_workflow"].eligibility_decision_policy
            assert decision.cutoff == expected_threshold
            assert decision.mode == ("top_fraction" if stage == "prescreen" else "confidence_threshold")
    assert len({call["silver_record_id"] for call in calls}) == 4
    with pytest.raises(ValueError, match="differs"):
        policy.evaluate_for_stage("final")(stage="screen", paper_id="paper-1")


def test_week_one_full_judges_do_not_require_decisions_credential():
    policy = FrozenEvaluationPolicy(
        round_id="week1", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION,
        adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="",
    )
    policy.evaluate_for_stage("week_one")
    with pytest.raises(ValueError, match="credential"):
        policy.evaluate_for_stage("screen")


def test_routing_settings_must_match_frozen_configuration():
    with pytest.raises(ValueError, match="frozen"):
        FrozenEvaluationPolicy(
            round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
            configuration=CONFIGURATION, adjudication_passes_by_judge=PANELS,
            source_context_for_paper=lambda _: {}, decision_api_key="private-key",
            screen_threshold=0.8,
        )


def test_policy_uses_its_frozen_tiebreak_judge_for_gold_audit():
    configuration = _configuration(TIEBREAK)
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(configuration),
        configuration=configuration, adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="private-key",
        tiebreak_pass=TIEBREAK,
    )
    sealed_case = {
        "case_id": "gold-1", "paper_id": "paper-1", "challenger_hotkey": "challenger",
        "case_public": {"question": "Is this supported?", "statement": "A improved survival.",
                        "source_quotes": ["The trial found greater survival with A."],
                        "source_span_ids": ["span-1"], "claim_side": "incumbent"},
    }
    assert policy.judge_known_answer(sealed_case)["choice"] == "incumbent"


def test_policy_rejects_substituted_judge_after_freeze():
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION, adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="private-key",
    )
    substitute = StaticAdjudicationPass("J2-1", "profile-J2", "other-model", {})
    PANELS["J2"] = (substitute, PANELS["J2"][1])
    try:
        with pytest.raises(ValueError, match="judge runtime"):
            policy.evaluate_for_stage("final")(
                stage="final", paper_id="paper-1", judge_id="J2",
            )
    finally:
        PANELS["J2"] = (StaticAdjudicationPass(
            "J2-1", "profile-J2", "model-J2", {},
        ), PANELS["J2"][1])


def test_pass_ids_alone_do_not_make_two_distinct_judges():
    same_model_panels = {
        "J1": PANELS["J1"],
        "J2": tuple(StaticAdjudicationPass(
            f"J2-{index}", "profile-J1", "model-J1", {},
        ) for index in (1, 2)),
    }
    configuration = {"decision_routing": ROUTING, "silver_runtime": silver_runtime_config(),
                     "workflow_specs": validated_workflow_spec(None),
                     "competition_policy_runtime": policy_runtime_config(),
                     **judge_runtime_config(same_model_panels, None)}
    with pytest.raises(ValueError, match="distinct"):
        FrozenEvaluationPolicy(
            round_id="week2", configuration_sha256=canonical_sha256(configuration),
            configuration=configuration, adjudication_passes_by_judge=same_model_panels,
            source_context_for_paper=lambda _: {}, decision_api_key="private-key",
        )


def test_embedding_model_change_after_freeze_stops_scoring(monkeypatch):
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION, adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="private-key",
    )
    monkeypatch.setenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL", "changed-model")
    with pytest.raises(ValueError, match="Silver runtime"):
        policy.evaluate_for_stage("final")(stage="final", paper_id="paper-1", judge_id="J1")


def test_silver_prompt_source_change_after_freeze_stops_scoring(monkeypatch):
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION, adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="private-key",
    )
    original = Path.read_bytes

    def changed_source(path):
        content = original(path)
        return content + b"\n" if path.name == "adjudication_passes.py" else content

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    with pytest.raises(ValueError, match="Silver runtime"):
        policy.evaluate_for_stage("final")(stage="final", paper_id="paper-1", judge_id="J1")


def test_competition_policy_source_change_after_freeze_stops_scoring(monkeypatch):
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION, adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="private-key",
    )
    original = Path.read_bytes

    def changed_source(path):
        content = original(path)
        return content + b"\n" if path.name == "policy.py" else content

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    with pytest.raises(ValueError, match="runtime"):
        policy.evaluate_for_stage("final")(stage="final", paper_id="paper-1", judge_id="J1")


def test_enabled_embeddings_require_trusted_credential(monkeypatch):
    monkeypatch.setenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODE", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    configuration = _configuration()
    with pytest.raises(ValueError, match="credential"):
        FrozenEvaluationPolicy(
            round_id="week2", configuration_sha256=canonical_sha256(configuration),
            configuration=configuration, adjudication_passes_by_judge=PANELS,
            source_context_for_paper=lambda _: {}, decision_api_key="private-key",
        )
