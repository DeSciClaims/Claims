from competition.gold import (
    GoldCase,
    GoldVote,
    VoteDecision,
    decide_gold_case,
    draw_gold_cases,
    escalated_choice,
    gold_result,
)


def case(index=1):
    return GoldCase(f"case-{index}", "paper", "challenger", "incumbent",
                    frozenset({"challenger-team", "incumbent-team"}))


def test_gold_draw_is_order_independent_and_uses_one_case_once():
    candidates = [case(index) for index in range(500)]
    first = draw_gold_cases(candidates, seed=b"s" * 32)
    assert len(first) == 300
    assert first == draw_gold_cases(list(reversed(candidates)), seed=b"s" * 32)
    assert first != draw_gold_cases(candidates, seed=b"t" * 32)


def test_only_qualified_outside_team_votes_count_and_three_quarters_decides():
    voters = {f"v{index}": f"team-{index}" for index in range(11)}
    voters["excluded"] = "challenger-team"
    votes = [GoldVote(f"v{index}", "incumbent" if index < 9 else "challenger")
             for index in range(11)]
    votes.extend([GoldVote("excluded", "challenger"), GoldVote("unqualified", "challenger")])
    decision = decide_gold_case(case(), votes, qualified_team_by_hotkey=voters)
    assert decision == VoteDecision("incumbent", 11, 9)
    assert escalated_choice(tiebreak="challenger", vote_decision=decision) == "incumbent"
    undecided = decide_gold_case(case(), votes[:10], qualified_team_by_hotkey=voters)
    assert undecided.choice is None
    assert escalated_choice(tiebreak="challenger", vote_decision=undecided) == "challenger"


def test_gold_refusal_requires_sixty_decided_contested_claims_and_75_percent_incumbent():
    decisions = [VoteDecision("incumbent", 11, 9)] * 45 + [VoteDecision("challenger", 11, 9)] * 15
    result = gold_result(decisions + [VoteDecision("neither", 11, 9)])
    assert result.sampled_cases == 61
    assert result.decided_contested_claims == 60
    assert result.incumbent_share == 0.75
    assert result.refusal is True
    assert gold_result(decisions[:59]).refusal is False
