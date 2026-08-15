from __future__ import annotations

from validator.agent_v1.record_projection import project_agent_artifact


def test_projection_excludes_every_claim_with_an_ambiguous_source_id() -> None:
    artifact = {
        "paper": {"paper_id": "paper"},
        "logic": {
            "claims": [
                {"claim_id": "C01", "statement": "First meaning."},
                {"claim_id": "C01", "statement": "Different meaning."},
                {"claim_id": "C02", "statement": "Unambiguous claim."},
            ]
        },
    }

    candidates = project_agent_artifact(artifact, origin="miner", miner_id="uid_20")

    assert [candidate.candidate_id for candidate in candidates] == ["miner:uid_20:C02"]
    assert candidates[0].statement == "Unambiguous claim."


def test_projection_keeps_unique_claim_ids_stable() -> None:
    artifact = {
        "paper": {"paper_id": "paper"},
        "logic": {
            "claims": [
                {"claim_id": "C01", "statement": "First claim."},
                {"claim_id": "C02", "statement": "Second claim."},
            ]
        },
    }

    candidates = project_agent_artifact(artifact, origin="miner", miner_id="uid_9")

    assert [candidate.candidate_id for candidate in candidates] == [
        "miner:uid_9:C01",
        "miner:uid_9:C02",
    ]
