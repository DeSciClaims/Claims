from copy import deepcopy

import pytest

from competition.review_policy import ReviewPolicy, review_policy
from competition.gold import GoldCase, GoldVote, VoteDecision, decide_gold_case, gold_result
from competition.gold_controller import closed_gold_decisions, tally_closed_gold_review
from competition.round_setup import prepare_round


def smoke_record():
    raw = ReviewPolicy().public()
    raw.update(reviewers_per_round=4, vote_quorum=3, genuine_cases_per_round=2,
               synthetic_cases_per_bucket=1, response_deadline_seconds=180,
               check_sample_size=4, check_min_decided=4, escalation_limit=16)
    return {"network": "testnet", "rules_public": {"policy": {
        "evaluation": {"mode": "testnet_smoke"}, "schedule": {"mode": "custom"},
        "consensus": raw,
    }}}


def test_legacy_policy_and_smoke_threshold_boundaries():
    assert review_policy({}) == ReviewPolicy()
    policy = review_policy(smoke_record())
    case = GoldCase("c", "p", "challenger", "incumbent", frozenset({"excluded"}))
    teams = {f"v{i}": f"t{i}" for i in range(4)}
    votes = [GoldVote(f"v{i}", "incumbent" if i < 3 else "challenger") for i in range(4)]
    assert decide_gold_case(case, votes, qualified_team_by_hotkey=teams,
                            quorum=policy.vote_quorum).choice == "incumbent"
    assert decide_gold_case(case, votes[:2], qualified_team_by_hotkey=teams,
                            quorum=policy.vote_quorum).choice is None
    split = votes[:2] + [GoldVote("v2", "challenger"), votes[3]]
    assert decide_gold_case(case, split, qualified_team_by_hotkey=teams,
                            quorum=policy.vote_quorum).choice is None
    decisions = [VoteDecision("incumbent", 4, 3)] * 3 + [VoteDecision("challenger", 4, 3)]
    assert gold_result(decisions, min_decided=policy.check_min_decided).refusal
    assert not gold_result(decisions[:3], min_decided=policy.check_min_decided).refusal
    assert not gold_result(decisions).refusal


@pytest.mark.parametrize("change", [
    {"network": "mainnet"},
    {"rules_public": {"policy": {"consensus": smoke_record()["rules_public"]["policy"]["consensus"]}}},
])
def test_reduced_thresholds_cannot_escape_smoke_round(change):
    with pytest.raises(ValueError, match="testnet smoke"):
        review_policy({**smoke_record(), **change})


@pytest.mark.parametrize("key,value", [
    ("vote_quorum", True), ("vote_quorum", 5), ("agreement_percent", 50),
    ("check_min_decided", 5), ("response_deadline_seconds", 59),
    ("controlled_test_accounts", [{"hotkey": "invalid", "coldkey": "invalid", "team_id": "test"}]),
])
def test_bad_thresholds_and_account_entries_are_rejected(key, value):
    record = smoke_record()
    record["rules_public"]["policy"]["consensus"][key] = value
    with pytest.raises(ValueError):
        review_policy(record)


def test_primary_controller_uses_frozen_smoke_policy_and_retains_team_exclusions():
    record = {**smoke_record(), "status": "gold_closed"}
    cases = [{"case_id": f"c{i}", "round_id": "round", "paper_id": "p", "challenger_hotkey": "challenger",
              "incumbent_hotkey": "incumbent", "excluded_team_ids": ["excluded"],
              "answer_private": {"review_purposes": ["check"]}} for i in range(4)]
    voters = [{"voter_hotkey": f"v{i}", "team_id": f"t{i}"} for i in range(4)]
    voters.append({"voter_hotkey": "self", "team_id": "excluded"})
    votes = [{"case_id": row["case_id"], "voter_hotkey": f"v{i}", "choice": "incumbent"}
             for row in cases for i in range(3)]
    votes += [{"case_id": row["case_id"], "voter_hotkey": "self", "choice": "challenger"}
              for row in cases]

    class Backend:
        def get_round(self, _): return record
        def list_gold_cases(self, _, **kw): return cases
        def list_gold_voters(self, _): return voters
        def list_gold_votes(self, _): return votes
        def list_stages(self, _):
            return [{"stage": "final", "entries": [{"role": "challenger", "hotkey": "challenger"}]}]

    decisions = closed_gold_decisions("round", backend=Backend())
    assert all(row.qualified_vote_count == 3 for row in decisions.values())
    assert tally_closed_gold_review("round", backend=Backend())["challenger"].refusal


def test_review_policy_is_frozen_before_intake_and_cannot_be_changed(tmp_path):
    from test_competition_round_setup import _draft

    draft = _draft()
    draft["policy_public"]["consensus"] = ReviewPolicy().public()

    class Backend:
        def create_round(self, payload): return payload

    first = prepare_round(draft, backend=Backend(), directory=tmp_path)
    changed = deepcopy(draft)
    changed["policy_public"]["consensus"]["escalation_limit"] = 1600
    with pytest.raises(ValueError, match="already differs"):
        prepare_round(changed, backend=Backend(), directory=tmp_path)
    assert first["rules_sha256"]
