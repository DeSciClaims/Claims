import pytest
from validator.agent_v1.adjudication_passes import StaticAdjudicationPass

from competition.evaluation_policy import FrozenEvaluationPolicy
from competition.decision_router import PROMPT_SHA256
from competition.publication import canonical_sha256


ROUTING = {
    "provider": "openai", "model": "gpt-6-luna", "prompt_sha256": PROMPT_SHA256,
    "prescreen_confidence_threshold": 0.8, "screen_confidence_threshold": 0.9,
}
CONFIGURATION = {"decision_routing": ROUTING, "judges": ["J1", "J2"]}
PANELS = {"J1": (object(), object()), "J2": (object(), object())}


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
            assert captured["decision_router"] is None
        else:
            assert captured["decision_router"].threshold == expected_threshold
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
    tiebreak = StaticAdjudicationPass(
        "tie", "profile", "model", {}, default_disposition="accepted_improvement",
    )
    policy = FrozenEvaluationPolicy(
        round_id="week2", configuration_sha256=canonical_sha256(CONFIGURATION),
        configuration=CONFIGURATION, adjudication_passes_by_judge=PANELS,
        source_context_for_paper=lambda _: {}, decision_api_key="private-key",
        tiebreak_pass=tiebreak,
    )
    sealed_case = {
        "case_id": "gold-1", "paper_id": "paper-1", "challenger_hotkey": "challenger",
        "case_public": {"question": "Is this supported?", "statement": "A improved survival.",
                        "source_quotes": ["The trial found greater survival with A."],
                        "source_span_ids": ["span-1"], "claim_side": "incumbent"},
    }
    assert policy.judge_known_answer(sealed_case)["choice"] == "incumbent"
