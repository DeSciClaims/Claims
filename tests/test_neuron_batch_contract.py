from __future__ import annotations

from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import ClaimsTask, task_cache_key
from neurons.validator import _aggregate_scores


def test_claims_task_round_trips_batch_fields_to_synapse() -> None:
    task = ClaimsTask.from_dict(
        {
            "task_id": "task_abc",
            "run_id": "run_xyz",
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
    assert synapse.run_id == "run_xyz"
    assert synapse.batch_id == "batch_def"
    assert synapse.selection_seed == "seed_123"
    assert synapse.papers[0]["paper_id"] == "paper_001"
    assert synapse.papers[0]["source_url"] == "https://example.org/paper.pdf"


def test_batch_score_rule_supports_min_mean_and_median() -> None:
    assert _aggregate_scores([0.9, 0.7, 0.8], "min") == 0.7
    assert _aggregate_scores([0.9, 0.7, 0.8], "mean") == 0.8
    assert _aggregate_scores([0.9, 0.7, 0.8], "median") == 0.8
    assert _aggregate_scores([], "min") == 0.0


def test_task_cache_key_reuses_same_paper_across_batches() -> None:
    first = ClaimsTask.from_dict(
        {
            "task_id": "task_1",
            "batch_id": "batch_1",
            "paper_id": "paper_001",
            "paper_url": "https://example.org/paper.pdf",
            "source_sha256": "abc123",
        }
    )
    second = ClaimsTask.from_dict(
        {
            "task_id": "task_2",
            "batch_id": "batch_2",
            "paper_id": "paper_001",
            "paper_url": "https://example.org/paper.pdf",
            "source_sha256": "abc123",
        }
    )

    assert task_cache_key(first, miner_version="agent_v1", model_config="model") == task_cache_key(
        second,
        miner_version="agent_v1",
        model_config="model",
    )
