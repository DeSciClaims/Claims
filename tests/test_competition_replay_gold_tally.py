"""Replay Gold signatures are independent from primary Gold signatures."""

import hashlib

import pytest
from bittensor_wallet import Keypair

from competition.replay_gold_tally import replay_gold_vote_message, tally_replay_gold_review


def _fixture():
    keys = [Keypair.create_from_mnemonic(Keypair.generate_mnemonic()) for _ in range(11)]
    voters = [{"voter_hotkey": key.ss58_address, "team_id": f"team-{index}"}
              for index, key in enumerate(keys)]
    cases = [{
        "case_id": "replay_gold_check", "paper_id": "paper-1",
        "challenger_hotkey": "challenger", "incumbent_hotkey": "reference",
        "excluded_team_ids": ["challenger-team", "reference-team"],
        "review_purposes": ["check"], "answer_private": {"tiebreak": "reference"},
    }, {
        "case_id": "replay_gold_challenge", "paper_id": "paper-1",
        "challenger_hotkey": "challenger", "incumbent_hotkey": "reference",
        "excluded_team_ids": ["challenger-team", "reference-team"],
        "review_purposes": ["challenge"],
        "answer_private": {"kind": "known_answer", "correct_choice": "challenger"},
    }]
    review = {"schema_version": "claims_replay_gold_review_v1",
              "round_id": "week-1", "finalist_hotkeys": ["challenger"],
              "cases": cases}
    votes = []
    for case in cases:
        for key in keys:
            message = replay_gold_vote_message(
                "week-1", case["case_id"], key.ss58_address, "challenger",
            )
            votes.append({
                "case_id": case["case_id"], "voter_hotkey": key.ss58_address,
                "choice": "challenger", "signed_payload_sha256": hashlib.sha256(message).hexdigest(),
                "signature_hex": key.sign(message).hex(),
            })
    return review, voters, votes


def test_replay_gold_tally_checks_own_signatures_and_known_answers():
    review, voters, votes = _fixture()
    result = tally_replay_gold_review(review, voters=voters, votes=votes)
    assert result["gold_by_hotkey"]["challenger"]["decided_contested_claims"] == 1
    assert result["gold_by_hotkey"]["challenger"]["refusal"] is False
    assert all(row == {"correct": 1, "total": 1}
               for row in result["challenge_accuracy"].values())
    assert len(result["evidence_sha256"]) == 64
    assert tally_replay_gold_review(
        review, voters=list(reversed(voters)), votes=list(reversed(votes)),
    ) == result
    changed = [dict(row) for row in votes]
    changed[0]["choice"] = "incumbent"
    with pytest.raises(ValueError, match="signature is invalid"):
        tally_replay_gold_review(review, voters=voters, votes=changed)
    review["cases"][0]["case_id"] = "gold_primary"
    with pytest.raises(ValueError, match="case is malformed"):
        tally_replay_gold_review(review, voters=voters, votes=votes)
