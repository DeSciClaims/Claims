"""A second validator's job and score receipts stay outside the primary backend."""

import sqlite3
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace

import pytest

from competition.jobs import build_jobs
from competition.paper_score_controller import score_ready_paper
from competition.publication import canonical_sha256
from competition.replay_plan import ReplayInitialPlan
from competition.replay_store import ReplayStore
from competition.sample_release import PaperRecord, frozen_pool_hash


NOW = datetime(2026, 10, 30, 14, tzinfo=UTC)


def _inputs():
    rules = {"configuration": {"worker_policy": {"allowed_models": ["model-a"]}}}
    sample = [{"stage": "week_one", "paper_id": "paper-1", "position": 0,
               "judge_id": "J1", "source_sha256": "e" * 64}]
    entries = [
        {"hotkey": "reference", "package_sha256": "c" * 64,
         "team_id": "team-reference", "role": "reference", "position": 0},
        {"hotkey": "challenger", "package_sha256": "d" * 64,
         "team_id": "team-challenger", "role": "challenger", "position": 1},
    ]
    snapshot = {
        "schema_version": "claims_competition_replay_snapshot_v1",
        "round_id": "round-1", "network": "testnet",
        "rules_public": rules, "rules_sha256": canonical_sha256(rules),
        "configuration_sha256": canonical_sha256(rules["configuration"]),
        "paper_pool_sha256": frozen_pool_hash([PaperRecord("paper-1", "e" * 64)]),
        "drand_seed": "b" * 64,
        "archive_block_hash": "0x" + "f" * 64,
        "deadline_block": 100, "reveal_block": 700,
        "duplicate_review_links": [],
        "duplicate_review_sha256": canonical_sha256([]),
        "incumbent_hotkey": None,
        "baseline_package_sha256": "c" * 64,
        "reference_package_sha256": "c" * 64,
        "reference_release_id": "release-1",
        "uploads": [{"hotkey": "challenger", "package_sha256": "d" * 64,
                     "package_object_key": "sealed/challenger", "package_size_bytes": 100,
                     "manifest": {"entrypoint": "miner:main"},
                     "salt_hex": "e" * 64, "commitment": "f" * 64,
                     "commit_block": 90, "status": "accepted"}],
        "sample": sample, "stages": [],
    }
    jobs = build_jobs(
        round_id="round-1", selected=entries, sample=sample,
        stage="week_one", configuration_sha256=snapshot["configuration_sha256"],
    )
    plan = ReplayInitialPlan(
        "round-1", "week_one", ("challenger",), (), tuple(entries), tuple(jobs),
    )
    return snapshot, plan


def _result():
    return {"status": "completed", "artifact": {"paper": {"paper_id": "paper-1"}},
            "inference_cost_usd": 0.12, "prompt_and_completion_tokens": 100}


def _evaluation(**kwargs):
    assert kwargs["paper_id"] == "paper-1" and kwargs["judge_id"] == "J1"
    return SimpleNamespace(
        pair_rows=({
            "paper_id": "paper-1", "challenger_hotkey": "challenger",
            "baseline_hotkey": "reference", "challenger_score": 0.7,
            "baseline_score": 0.6, "infrastructure_fault": False,
            "details_public": {},
        },),
        pipeline=SimpleNamespace(
            paper_id="paper-1",
            scores=[SimpleNamespace(miner_id="challenger", score=0.7),
                    SimpleNamespace(miner_id="reference", score=0.6)],
            silver_record=SimpleNamespace(
                model_dump=lambda **_: {"id": "silver-1"}, silver_units=[],
            ),
            diff_cases=[], adjudication_consensus=[], miner_submissions=[],
        ),
        judge_usage_events=(),
    )


def test_replay_store_leases_baseline_first_and_freezes_scores_across_restart(tmp_path):
    snapshot, plan = _inputs()
    now = [NOW]
    path = tmp_path / "replay.sqlite"
    store = ReplayStore(path, clock=lambda: now[0])
    store.initialize(snapshot, plan)
    baseline = store.claim_job("round-1", worker_id="worker-1", lease_seconds=60)
    assert baseline["hotkey"] == "reference"
    assert store.claim_job("round-1", worker_id="worker-2", lease_seconds=60) is None
    store.complete_job(
        baseline["job_id"], worker_id="worker-1",
        lease_token=baseline["lease_token"], result=_result(),
        log_object_key="logs/reference",
    )
    challenger = store.claim_job("round-1", worker_id="worker-2", lease_seconds=60)
    assert challenger["hotkey"] == "challenger"
    store.complete_job(
        challenger["job_id"], worker_id="worker-2",
        lease_token=challenger["lease_token"], result=_result(),
        log_object_key="logs/challenger",
    )
    restarted = ReplayStore(path, clock=lambda: now[0])
    restarted.initialize(snapshot, plan)
    assert restarted.claim_job("round-1", worker_id="worker-3", lease_seconds=60) is None
    assert score_ready_paper(
        "round-1", "week_one", "paper-1", backend=restarted,
        bronze_artifact={"paper": {"paper_id": "paper-1"}}, evaluate=_evaluation,
    )["status"] == "scored"
    assert restarted.list_stage_pairs("round-1", "week_one")[0]["challenger_score"] == 0.7
    assert score_ready_paper(
        "round-1", "week_one", "paper-1", backend=ReplayStore(path),
        bronze_artifact={"paper": {"paper_id": "paper-1"}},
        evaluate=lambda **_: pytest.fail("frozen adjudication must be replayed"),
    )["status"] == "scored"
    with pytest.raises(ValueError, match="already bound"):
        restarted.initialize({**snapshot, "drand_seed": "changed"}, plan)
    assert path.stat().st_mode & 0o777 == 0o600


def test_replay_store_rejects_stale_lease_and_exhausts_retry(tmp_path):
    snapshot, plan = _inputs()
    now = [NOW]
    store = ReplayStore(tmp_path / "replay.sqlite", clock=lambda: now[0])
    store.initialize(snapshot, plan)
    first = store.claim_job("round-1", worker_id="worker", lease_seconds=10)
    now[0] += timedelta(seconds=11)
    second = store.claim_job("round-1", worker_id="worker", lease_seconds=10)
    assert second["job_id"] == first["job_id"] and second["attempts"] == 2
    with pytest.raises(ValueError, match="no longer valid"):
        store.complete_job(
            first["job_id"], worker_id="worker", lease_token=first["lease_token"],
            result=_result(), log_object_key="logs/old",
        )
    now[0] += timedelta(seconds=11)
    assert store.claim_job("round-1", worker_id="worker", lease_seconds=10) is None
    jobs = store.list_paper_jobs("round-1", "week_one", "paper-1")
    baseline = next(job for job in jobs if job["hotkey"] == "reference")
    assert baseline["result_private"]["status"] == "infrastructure_fault"


def test_replay_store_rejects_changed_job_grid(tmp_path):
    snapshot, plan = _inputs()
    store = ReplayStore(tmp_path / "replay.sqlite")
    with pytest.raises(ValueError, match="job grid differs"):
        store.initialize(snapshot, replace(plan, jobs=plan.jobs[:-1]))


def test_replay_store_seeds_stage_receipt_for_existing_database(tmp_path):
    snapshot, plan = _inputs()
    path = tmp_path / "replay.sqlite"
    store = ReplayStore(path)
    store.initialize(snapshot, plan)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM replay_stages")
    restarted = ReplayStore(path)
    restarted.initialize(snapshot, plan)
    assert restarted.list_stages("round-1") == [{
        "stage": "week_one", "entries": list(plan.entries),
    }]
    assert len(restarted.list_paper_jobs("round-1", "week_one", "paper-1")) == 2
