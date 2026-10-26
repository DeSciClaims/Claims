"""Primary votes must never be assigned by a stochastic case ID alone."""

import pytest

from competition.gold import GoldCase
from competition.gold_controller import ContestedClaim
from competition.replay_gold_alignment import align_closed_gold_prompts


def _local(case_id, *, paper="paper-1", statement="Claim"):
    return ContestedClaim(
        GoldCase(case_id, paper, "miner-1", "reference", frozenset({"team-1"})),
        {"statement": statement, "claim_side": "challenger"},
        {"kind": "silver_unit"},
    )


def _primary(case_id, *, paper="paper-1", statement="Claim", purpose="check"):
    return {
        "case_id": case_id, "paper_id": paper,
        "challenger_hotkey": "miner-1", "incumbent_hotkey": "reference",
        "case_public": {"statement": statement, "claim_side": "challenger"},
        "review_purposes": [purpose],
    }


def test_alignment_matches_identical_prompt_with_different_case_ids_only_once():
    result = align_closed_gold_prompts(
        [_local("local-1"), _local("local-2", paper="paper-2")],
        {"cases": [_primary("primary-1"), _primary("primary-2", paper="paper-3"),
                   _primary("challenge-1", purpose="challenge")]},
    )
    assert result == {
        "local_case_count": 2, "primary_case_count": 2,
        "exact_matches": [{"local_case_id": "local-1", "primary_case_id": "primary-1",
                           "primary_review_purposes": ["check"]}],
        "ambiguous_local_case_count": 0, "unmatched_local_case_count": 1,
    }


def test_alignment_never_assigns_ambiguous_or_changed_prompt():
    result = align_closed_gold_prompts(
        [_local("local-1"), _local("local-2"),
         _local("local-3", statement="Changed")],
        {"cases": [_primary("primary-1")]},
    )
    assert result["exact_matches"] == []
    assert result["ambiguous_local_case_count"] == 2
    assert result["unmatched_local_case_count"] == 3


def test_alignment_rejects_duplicate_case_identity():
    with pytest.raises(ValueError, match="repeated"):
        align_closed_gold_prompts([_local("same"), _local("same")], {"cases": []})
