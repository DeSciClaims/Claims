from __future__ import annotations

from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import ClaimsTask
from neurons.validator import _aggregate_scores


def test_claims_task_round_trips_batch_fields_to_synapse() -> None:
    task = ClaimsTask.from_dict(
        {
            "task_id": "task_abc",
            "batch_id": "batch_def",
            "selection_seed": "seed_123",
            "task_version": "claims_task_v0",
            "scoring_version": "agent_v1_pass4_deterministic_v0",
            "network": "testnet",
            "netuid": 530,
            "papers": [
                {
                    "paper_id": "paper_001",
                    "title": "Demo Paper",
                    "source_url": "https://example.org/paper.pdf",
                    "source_sha256": "abc123",
                    "topics": ["demo"],
                }
            ],
        }
    )

    synapse = ClaimExtractionSynapse(**task.to_synapse_kwargs())

    assert synapse.task_id == "task_abc"
    assert synapse.batch_id == "batch_def"
    assert synapse.selection_seed == "seed_123"
    assert synapse.papers[0]["paper_id"] == "paper_001"
    assert synapse.papers[0]["source_url"] == "https://example.org/paper.pdf"


def test_batch_score_rule_defaults_to_highest_minimum_signal() -> None:
    assert _aggregate_scores([0.9, 0.7, 0.8], "min") == 0.7
    assert _aggregate_scores([0.9, 0.7, 0.8], "mean") == 0.8
    assert _aggregate_scores([0.9, 0.7, 0.8], "median") == 0.8
    assert _aggregate_scores([], "min") == 0.0
