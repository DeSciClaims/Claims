import hashlib

import pytest

from competition.drand import DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, fetch_verified_beacon, first_round_after


def test_first_beacon_is_strictly_after_chain_block_time():
    assert first_round_after(99, genesis_time=100, period_seconds=30) == 1
    assert first_round_after(100, genesis_time=100, period_seconds=30) == 2
    assert first_round_after(129, genesis_time=100, period_seconds=30) == 2
    assert first_round_after(130, genesis_time=100, period_seconds=30) == 3


def test_drand_cli_beacon_is_pinned_and_hash_checked():
    signature = b"a" * 96
    calls = []

    def cli(command):
        calls.append(command)
        if "chain-info" in command:
            return {"hash": DEFAULT_CHAIN_HASH, "public_key": DEFAULT_PUBLIC_KEY,
                    "genesis_time": 100, "period": 30}
        return {"round": 3, "signature": signature.hex(),
                "randomness": hashlib.sha256(signature).hexdigest()}

    result = fetch_verified_beacon(block_timestamp=130, run_cli=cli)
    assert result.round == 3 and result.scheduled_at == 160
    assert result.seed == hashlib.sha256(signature).digest()
    assert calls[1][-1] == "3"

    def wrong_identity(command):
        return {"hash": "0" * 64, "public_key": DEFAULT_PUBLIC_KEY,
                "genesis_time": 100, "period": 30}

    with pytest.raises(RuntimeError, match="identity"):
        fetch_verified_beacon(block_timestamp=130, run_cli=wrong_identity)
