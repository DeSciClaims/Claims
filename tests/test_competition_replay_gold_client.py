"""A signed replay review can be opened and closed without a primary token."""

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

from bittensor_wallet import Keypair

from competition.replay_gold_client import ReplayGoldClient
from competition.replay_gold_tally import replay_gold_vote_message


def test_replay_gold_client_signs_opening_and_verifies_closed_votes():
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    voter = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    review = {
        "schema_version": "claims_replay_gold_review_v1", "round_id": "week-1",
        "finalist_hotkeys": ["challenger"],
        "cases": [{"case_id": "replay_gold_case1", "paper_id": "paper-1",
                   "challenger_hotkey": "challenger", "incumbent_hotkey": "reference",
                   "excluded_team_ids": ["challenger-team", "reference-team"],
                   "review_purposes": ["check"], "answer_private": {}}],
    }
    voters = [{"voter_hotkey": voter.ss58_address, "team_id": "voter-team"}]
    opened = {}

    def respond(request, *, timeout):
        if request.get_method() == "POST" and request.full_url.endswith("/open"):
            assert timeout == 60
            receipt = json.loads(request.data)
            digest = receipt["bundle_sha256"]
            assert receipt["signer_hotkey"] == signer.ss58_address
            assert Keypair(ss58_address=signer.ss58_address).verify(
                bytes.fromhex(digest), bytes.fromhex(receipt["signature_hex"]),
            )
            opened.update(receipt)
            return io.BytesIO(json.dumps({
                "round_id": "week-1", "review_sha256": digest,
                "status": "open", "closes_at": "2026-11-03T12:00:00+00:00",
            }).encode())
        assert request.get_method() == "GET" and timeout == 30
        assert request.get_header("Authorization") == "Bearer replay-only"
        case_id = review["cases"][0]["case_id"]
        message = replay_gold_vote_message("week-1", case_id,
                                           voter.ss58_address, "challenger")
        return io.BytesIO(json.dumps({
            "round_id": "week-1", "receipt": opened,
            "votes": [{"case_id": case_id, "voter_hotkey": voter.ss58_address,
                       "choice": "challenger",
                       "signed_payload_sha256": hashlib.sha256(message).hexdigest(),
                       "signature_hex": voter.sign(message).hex()}],
        }).encode())

    client = ReplayGoldClient(
        "https://backend.example", "replay-only", signer, open_request=respond,
    )
    result = client.open("week-1", review=review, voters=voters)
    assert result["status"] == "open"
    closed = client.closed("week-1", expected_review_sha256=result["review_sha256"])
    assert closed["tally"]["gold_by_hotkey"]["challenger"]["sampled_cases"] == 1
    assert client.close_when_due(
        "week-1", closes_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    ) is False
