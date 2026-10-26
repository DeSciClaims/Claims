from bittensor_wallet import Keypair

from competition.publication import canonical_sha256, publication_bundle, sign_publication


def test_publication_hash_and_hotkey_signature_are_reproducible():
    round_record = {
        "round_id": "week-1", "network": "testnet",
        "archive_block_hash": "0x" + "a" * 64,
        "drand_round": 10, "drand_seed": "b" * 64,
        "paper_pool_sha256": "c" * 64, "rules_sha256": "d" * 64,
        "configuration_sha256": "e" * 64,
    }
    bundle = publication_bundle(round_record, results={"decision": "crowned"}, weights={"vector": [{"uid": 0, "weight": 0.1}]})
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    receipt = sign_publication(bundle, signer)
    assert receipt["bundle_sha256"] == canonical_sha256(bundle)
    assert Keypair(ss58_address=receipt["signer_hotkey"]).verify(
        bytes.fromhex(receipt["bundle_sha256"]), bytes.fromhex(receipt["signature_hex"]),
    )
    assert canonical_sha256({**bundle, "results": {"decision": "rejected"}}) != receipt["bundle_sha256"]
