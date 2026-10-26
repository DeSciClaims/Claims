"""A replay Gold draw must use local cases and its own vote namespace."""

from types import SimpleNamespace

import pytest

from competition.gold import GoldCase
from competition.gold_controller import ContestedClaim
from competition.replay_gold_review import prepare_replay_gold_review


def _backend():
    stored = {}
    source_cases = [
        {
            "case": {"case_id": f"silver-{index}", "paper_id": "paper-1",
                     "challenger_hotkey": "challenger", "incumbent_hotkey": "reference",
                     "excluded_team_ids": ["challenger-team", "reference-team"]},
            "case_public": {"statement": f"Claim {index}"},
            "answer_private": {"tiebreak": "challenger", "primary_split": index == 0},
        }
        for index in range(3)
    ]

    def freeze(_, review):
        if stored and stored["review"] != review:
            raise ValueError("replay Gold review was already frozen differently")
        stored["review"] = review
        return review

    backend = SimpleNamespace(
        snapshot={"round_id": "week-1", "drand_seed": "ab" * 32},
        list_stages=lambda _: [{"stage": "week_one", "entries": [
            {"hotkey": "reference", "role": "reference", "team_id": "reference-team",
             "package_sha256": "a" * 64},
            {"hotkey": "challenger", "role": "challenger", "team_id": "challenger-team",
             "package_sha256": "b" * 64},
        ]}],
        list_sample=lambda _: [{"stage": "week_one", "paper_id": "paper-1"}],
        get_paper_evaluation=lambda *_: {"result_private": {
            "silver": {"gold_candidates": source_cases},
        }},
        get_final_audit=lambda _: {"input_sha256": "a" * 64},
        freeze_gold_review=freeze,
    )
    return backend


def _challenge():
    return ContestedClaim(
        GoldCase("challenge-1", "paper-1", "challenger", "reference",
                 frozenset({"challenger-team", "reference-team"})),
        {"question": "Does this match the evidence?", "statement": "Hidden answer",
         "source_quotes": ["Evidence"], "source_span_ids": ["span-1"],
         "claim_side": "challenger", "challenger_hotkey": "challenger",
         "incumbent_hotkey": "reference"},
        {"kind": "known_answer", "correct_choice": "challenger",
         "source_challenge_id": "certified-1", "certificate_sha256": "c" * 64},
    )


def test_replay_gold_draw_is_own_deterministic_immutable_case_set():
    backend = _backend()
    args = dict(backend=backend, known_answers=[_challenge()],
                check_limit=2, escalation_limit=1, minimum_known_answers=1)
    review = prepare_replay_gold_review("week-1", **args)
    assert prepare_replay_gold_review("week-1", **args) == review
    assert review["check_count"] == 2
    assert review["escalation_count"] == 1
    assert review["known_answer_count"] == 1
    assert all(row["case_id"].startswith("replay_gold_") for row in review["cases"])
    assert all(row["case_id"] != row["source_case_id"] for row in review["cases"])
    assert any("challenge" in row["review_purposes"] for row in review["cases"])
    with pytest.raises(ValueError, match="frozen differently"):
        prepare_replay_gold_review("week-1", **{**args, "check_limit": 1})


def test_replay_gold_refuses_missing_certified_challenge():
    with pytest.raises(ValueError, match="certified known-answer"):
        prepare_replay_gold_review(
            "week-1", backend=_backend(), known_answers=[],
            minimum_known_answers=1,
        )
