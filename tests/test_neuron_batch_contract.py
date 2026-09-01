from __future__ import annotations

from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import SCORING_VERSION, ClaimsTask, task_cache_key
from neurons.validator import _aggregate_scores


def test_claims_task_round_trips_batch_fields_to_synapse() -> None:
    task = ClaimsTask.from_dict(
        {
            "task_id": "task_abc",
            "run_id": "run_xyz",
            "batch_id": "batch_def",
            "assignment_key": "assignment_123",
            "assignment_window_start": "2026-08-25T06:00:00+00:00",
            "selection_seed": "seed_123",
            "miner_selection_recent_registration_block": 987_654,
            "task_version": "claims_task_v0",
            "scoring_version": "agent_v1_pass4_deterministic_v0",
            "network": "testnet",
            "netuid": 530,
            "target_uids": [7, 8],
            "target_miners": [
                {"uid": 7, "hotkey": "hotkey_7", "registration_block": 100},
                {"uid": 8, "hotkey": "hotkey_8", "registration_block": 100},
            ],
            "metagraph_block": 1_000,
            "miner_selection_algorithm": "uid_v0",
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
    assert synapse.assignment_key == "assignment_123"
    assert synapse.assignment_window_start == "2026-08-25T06:00:00+00:00"
    assert task.assignment_key == "assignment_123"
    assert task.assignment_window_start == "2026-08-25T06:00:00+00:00"
    assert synapse.selection_seed == "seed_123"
    assert task.miner_selection_recent_registration_block == 987_654
    assert "miner_selection_recent_registration_block" not in task.to_synapse_kwargs()
    assert task.target_uids == (7, 8)
    assert [item["uid"] for item in task.target_miners] == [7, 8]
    assert task.metagraph_block == 1_000
    assert task.miner_selection_algorithm == "uid_v0"
    assert synapse.papers[0]["paper_id"] == "paper_001"
    assert synapse.papers[0]["source_url"] == "https://example.org/paper.pdf"


def test_claims_task_uses_current_scoring_version_by_default() -> None:
    task = ClaimsTask.from_dict({"task_id": "task_current"})

    assert task.scoring_version == SCORING_VERSION == "agent_v1_pass4_minor_cap_v1"


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
