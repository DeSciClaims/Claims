from competition.gold import VoteDecision
from competition.gold_override import rescore_final_paper_from_escalations
from competition.scoring import score_pairwise_paper
from validator.agent_v1.comparison_models import (
    ComparisonCandidate, InvalidMinerCandidate, SilverRecord, SilverUnit,
)


def _frozen_result():
    challenger = ComparisonCandidate(
        candidate_id="miner:challenger:c1", paper_id="paper-1", origin="miner",
        miner_id="challenger", record_id="c1", statement="A reduced mortality.",
        normalized_statement="a reduced mortality",
    )
    incumbent = ComparisonCandidate(
        candidate_id="miner:incumbent:i1", paper_id="paper-1", origin="miner",
        miner_id="incumbent", record_id="i1", statement="B improved survival.",
        normalized_statement="b improved survival",
    )
    silver = SilverRecord(
        silver_record_id="silver-1", paper_id="paper-1", silver_units=[
            SilverUnit(silver_unit_id="unit-c", paper_id="paper-1", statement="A reduced mortality.",
                       importance="central", equivalent_candidate_ids=[challenger.candidate_id],
                       adjudication_case_ids=["split-1"]),
            SilverUnit(silver_unit_id="unit-i", paper_id="paper-1", statement="B improved survival.",
                       importance="supporting", equivalent_candidate_ids=[incumbent.candidate_id]),
        ],
    )
    pair = score_pairwise_paper(
        challenger_id="challenger", incumbent_id="incumbent", silver_record=silver,
        challenger_candidates=[challenger], incumbent_candidates=[incumbent],
    )
    return {
        "pairs": [{
            "paper_id": "paper-1", "challenger_hotkey": "challenger",
            "baseline_hotkey": "incumbent", "challenger_score": pair.challenger.score,
            "baseline_score": pair.incumbent.score, "infrastructure_fault": False,
            "details_public": {"silver_record_id": "silver-1"},
        }],
        "silver": {
            "silver_record": silver.model_dump(mode="json"),
            "miner_submissions": [
                {"miner_id": "challenger", "candidates": [challenger.model_dump(mode="json")],
                 "normal_findings": []},
                {"miner_id": "incumbent", "candidates": [incumbent.model_dump(mode="json")],
                 "normal_findings": []},
            ],
        },
    }


def _case(purpose):
    return {
        "case_id": "gold-1", "paper_id": "paper-1", "challenger_hotkey": "challenger",
        "incumbent_hotkey": "incumbent", "case_public": {},
        "answer_private": {"silver_unit_id": "unit-c", "tiebreak": "challenger",
                           "review_purposes": [purpose]},
    }


def test_decided_escalation_rejects_contested_silver_credit():
    frozen = _frozen_result()
    original = frozen["pairs"][0]
    revised = rescore_final_paper_from_escalations(
        frozen, sealed_cases=[_case("escalation")],
        decisions={"gold-1": VoteDecision("incumbent", 12, 12)},
    )[0]
    assert revised["challenger_score"] < original["challenger_score"]
    assert revised["baseline_score"] > original["baseline_score"]
    assert revised["details_public"]["gold_override_count"] == 1
    assert frozen["pairs"][0] == original


def test_check_only_or_undecided_case_does_not_change_final_pair():
    frozen = _frozen_result()
    against = {"gold-1": VoteDecision("incumbent", 12, 12)}
    assert rescore_final_paper_from_escalations(
        frozen, sealed_cases=[_case("check")], decisions=against,
    ) == frozen["pairs"]
    assert rescore_final_paper_from_escalations(
        frozen, sealed_cases=[_case("escalation")],
        decisions={"gold-1": VoteDecision(None, 10, 7)},
    ) == frozen["pairs"]


def test_decided_escalation_accepts_rejected_claim_and_removes_penalty():
    frozen = _frozen_result()
    candidate = ComparisonCandidate(
        candidate_id="miner:challenger:c2", paper_id="paper-1", origin="miner",
        miner_id="challenger", record_id="c2", statement="C improved survival.",
        normalized_statement="c improved survival", importance="supporting",
    )
    frozen["silver"]["miner_submissions"][0]["candidates"].append(candidate.model_dump(mode="json"))
    silver = SilverRecord.model_validate(frozen["silver"]["silver_record"])
    silver.invalid_miner_candidates.append(InvalidMinerCandidate(
        candidate_id=candidate.candidate_id, miner_id="challenger",
        reason="Rejected by tiebreak", adjudication_case_id="split-2",
    ))
    frozen["silver"]["silver_record"] = silver.model_dump(mode="json")
    challenger = [ComparisonCandidate.model_validate(row)
                  for row in frozen["silver"]["miner_submissions"][0]["candidates"]]
    incumbent = [ComparisonCandidate.model_validate(row)
                 for row in frozen["silver"]["miner_submissions"][1]["candidates"]]
    original = score_pairwise_paper(
        challenger_id="challenger", incumbent_id="incumbent", silver_record=silver,
        challenger_candidates=challenger, incumbent_candidates=incumbent,
    )
    frozen["pairs"][0]["challenger_score"] = original.challenger.score
    frozen["pairs"][0]["baseline_score"] = original.incumbent.score
    case = {
        "case_id": "gold-invalid", "paper_id": "paper-1",
        "challenger_hotkey": "challenger", "incumbent_hotkey": "incumbent",
        "answer_private": {"kind": "invalid_candidate", "tiebreak": "incumbent",
                           "invalid_candidate_id": candidate.candidate_id,
                           "review_purposes": ["escalation"]},
    }
    revised = rescore_final_paper_from_escalations(
        frozen, sealed_cases=[case],
        decisions={"gold-invalid": VoteDecision("challenger", 12, 12)},
    )[0]
    assert revised["challenger_score"] > original.challenger.score
    assert revised["details_public"]["gold_override_count"] == 1
    assert rescore_final_paper_from_escalations(
        frozen, sealed_cases=[case],
        decisions={"gold-invalid": VoteDecision("incumbent", 12, 12)},
    ) == frozen["pairs"]
