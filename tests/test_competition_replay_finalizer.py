"""The independent controller publishes only after its separate Gold closes."""

from types import SimpleNamespace

import pytest

from bittensor_wallet import Keypair

from competition.final_selection import FinalSelection, FinalistVerdict
from competition.publication import canonical_sha256
from competition.replay_finalizer import finalize_replay


@pytest.mark.parametrize("reviewed", [True, False])
def test_replay_finalizer_waits_then_signs_own_gold_and_vector(monkeypatch, reviewed):
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    review = {"round_id": "week-1", "stage": "week_one", "cases": []}
    tally = {
        "review_sha256": canonical_sha256(review),
        "evidence_sha256": "3" * 64,
        "gold_by_hotkey": {"miner-1": {
            "sampled_cases": 0, "decided_contested_claims": 0,
            "incumbent_supported": 0, "incumbent_share": None, "refusal": False,
        }},
        "case_decisions": {},
    }
    pairs = [{"paper_id": "paper-1", "challenger_hotkey": "miner-1",
              "challenger_score": 0.7, "baseline_score": 0.5}]
    selection = FinalSelection(
        "miner-1", "new_crown", 0.2,
        (FinalistVerdict("miner-1", "team-1", 0.2, 0.01, 500,
                         True, "cleared"),), tuple(pairs),
    )
    seen = {"selected": None, "published": [], "receipt": None, "ready": False}

    def select(_round_id, **kwargs):
        seen["selected"] = kwargs
        return selection

    monkeypatch.setattr("competition.replay_finalizer.select_replay_final", select)

    def qualification(_round_id, *, hotkeys, deadline_at):
        assert hotkeys == ["miner-1"]
        assert deadline_at == "2026-10-30T12:00:00+00:00"
        return {"round_id": "week-1", "window_start": "2026-10-23T12:00:00+00:00",
                "cutoff": deadline_at,
                "hotkeys": [{"hotkey": "miner-1", "qualified": True,
                             "qualified_round_count": 2, "qualifying_rounds": []}]}

    backend = SimpleNamespace(
        snapshot={
            "round_id": "week-1", "network": "testnet", "rules_sha256": "a" * 64,
            "configuration_sha256": "b" * 64, "archive_block_hash": "0x" + "c" * 64,
            "drand_round": 42, "drand_seed": "d" * 64,
            "paper_pool_sha256": "e" * 64, "duplicate_review_sha256": "f" * 64,
            "baseline_package_sha256": "1" * 64,
            "reference_package_sha256": "1" * 64,
            "rules_public": {"deadline_at": "2026-10-30T12:00:00+00:00"},
        },
        get_replay_publication=lambda _: seen["receipt"],
        get_gold_review=lambda _: review,
        list_stages=lambda _: [{"stage": "week_one", "entries": [
            {"role": "reference", "hotkey": "reference", "team_id": "ref-team"},
            {"role": "challenger", "hotkey": "miner-1", "team_id": "team-1"},
        ]}],
        list_stage_pairs=lambda *_: pairs,
        list_sample=lambda _: [{"stage": "week_one", "paper_id": "paper-1"}],
        get_final_audit=lambda _: {"input_sha256": "2" * 64,
                                   "result_private": {"round_id": "week-1",
                                                      "stage": "week_one"}},
        inputs=SimpleNamespace(qualification_evidence=qualification),
        freeze_replay_publication=lambda _, receipt, **__: seen.update(receipt=receipt),
    )
    if not reviewed:
        backend.snapshot["rules_public"]["policy"] = {"winner_selection": "silver_tiebreak"}
        backend.get_gold_review = lambda _: pytest.fail("Silver-only replay must not read votes")
        tally["gold_by_hotkey"] = {}
    publisher = SimpleNamespace(
        primary_published=lambda _: seen["ready"],
        publish=lambda _, receipt, **__: seen["published"].append(receipt),
    )
    params = dict(
        backend=backend, tally=tally, signer=signer,
        publication_client=publisher,
        subtensor=SimpleNamespace(metagraph=lambda **_: SimpleNamespace(
            hotkeys=["owner", "operator", "miner-1"])),
        netuid=1, owner_hotkey="owner", operator_hotkey="operator",
        verify_signature=lambda hotkey, message, signature:
            bool(Keypair(ss58_address=hotkey).verify(message, signature)),
    )
    assert finalize_replay("week-1", **params)["status"] == "waiting_for_primary_publication"
    assert seen["selected"] is None
    seen["ready"] = True
    first = finalize_replay("week-1", **params)
    assert first["status"] == "independent_published"
    assert seen["selected"]["gold_review"] is (review if reviewed else None)
    if reviewed:
        assert seen["selected"]["gold_by_hotkey"]["miner-1"].refusal is False
    else:
        assert seen["selected"]["gold_by_hotkey"] == {}
        assert seen["receipt"]["bundle"]["gold_policy_id"] == "silver-tiebreak-v1"
    assert seen["receipt"]["bundle"]["weights"]["vector"] == [
        {"uid": 0, "weight": 0.03}, {"uid": 1, "weight": 0.9},
        {"uid": 2, "weight": 0.07},
    ]
    assert seen["receipt"]["bundle"]["gold_adjusted_pairs_sha256"] == canonical_sha256(pairs)
    if reviewed:
        assert seen["receipt"]["bundle"]["results"]["gold"]["miner-1"]["refusal"] is False
    else:
        assert seen["receipt"]["bundle"]["results"]["gold"] == {}
    assert finalize_replay("week-1", **params) == first
    assert len(seen["published"]) == 2
