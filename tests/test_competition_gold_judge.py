from validator.agent_v1.adjudication_passes import StaticAdjudicationPass

from competition.gold_judge import judge_known_answer_case


CASE = {
    "case_id": "gold-1", "paper_id": "paper-1", "challenger_hotkey": "challenger",
    "case_public": {"question": "Is this supported?", "statement": "A improved survival.",
                    "source_quotes": ["The trial found greater survival with A."],
                    "source_span_ids": ["span-1"], "claim_side": "incumbent"},
}


def test_tiebreak_judge_scores_same_sealed_case_for_both_orientations():
    supported = StaticAdjudicationPass(
        "tie", "profile", "model", {}, default_disposition="accepted_improvement",
    )
    supported_result = judge_known_answer_case(CASE, tiebreak_pass=supported)
    assert supported_result["choice"] == "incumbent"
    assert supported_result["model_runtime_id"] == "model"
    assert len(supported_result["vote_sha256"]) == 64
    rejected = StaticAdjudicationPass(
        "tie", "profile", "model", {}, default_disposition="miner_error",
    )
    assert judge_known_answer_case(CASE, tiebreak_pass=rejected)["choice"] == "challenger"
