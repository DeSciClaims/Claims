"""Signed replay publication sends no primary credential or private inputs."""

import io
import json

import pytest
from bittensor_wallet import Keypair

from competition.publication import canonical_sha256
from competition.replay_publication_client import ReplayPublicationClient


def test_replay_publication_client_posts_signed_receipt_only():
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    bundle = {"schema_version": "claims_competition_independent_replay_v1",
              "round_id": "week-1", "weights": {"vector": []}}
    digest = canonical_sha256(bundle)
    receipt = {"bundle": bundle, "bundle_sha256": digest,
               "signer_hotkey": signer.ss58_address,
               "signature_hex": signer.sign(bytes.fromhex(digest)).hex()}
    requests = []

    def respond(request, *, timeout):
        requests.append(request)
        assert timeout == 30 and request.get_method() == "POST"
        return io.BytesIO(json.dumps({
            "round_id": "week-1", "bundle_sha256": digest, "status": "published",
        }).encode())

    client = ReplayPublicationClient(
        "https://backend.example", signer.ss58_address, open_request=respond,
    )
    assert client.publish("week-1", receipt)["status"] == "published"
    assert requests[0].full_url.endswith("/rounds/week-1/independent-results")
    assert json.loads(requests[0].data) == receipt
    assert requests[0].get_header("Authorization") is None
    with pytest.raises(ValueError, match="expected round or signer"):
        client.publish("week-2", receipt)
