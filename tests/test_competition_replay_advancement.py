"""Replay finalists are chosen from its own durable paper scores."""

from types import SimpleNamespace

from competition.jobs import build_jobs
from competition.paper_score_controller import score_ready_paper
from competition.publication import canonical_sha256
from competition.replay_plan import ReplayInitialPlan
from competition.replay_store import ReplayStore
from competition.stage_controller import advance_ready_stage


def _screen_inputs():
    configuration = {"worker_policy": {"allowed_models": ["model-a"]}}
    hotkeys = ("miner-a", "miner-b", "miner-c", "miner-d")
    sample = [
        {"stage": "screen", "paper_id": "screen-paper", "position": 0,
         "judge_id": "J1", "source_sha256": "e" * 64},
        {"stage": "final", "paper_id": "final-paper", "position": 0,
         "judge_id": "J2", "source_sha256": "f" * 64},
    ]
    entries = [{"hotkey": "incumbent", "package_sha256": "1" * 64,
                "team_id": "team-incumbent", "role": "incumbent", "position": 0}]
    uploads = []
    for position, hotkey in enumerate(hotkeys, 1):
        digest = str(position + 1) * 64
        entries.append({"hotkey": hotkey, "package_sha256": digest,
                        "team_id": "team-" + hotkey, "role": "challenger",
                        "position": position})
        uploads.append({"hotkey": hotkey, "team_id": "team-" + hotkey,
                        "package_sha256": digest, "commit_block": 90 + position,
                        "status": "accepted"})
    snapshot = {
        "round_id": "round-2", "network": "testnet",
        "rules_sha256": "a" * 64,
        "configuration_sha256": canonical_sha256(configuration),
        "paper_pool_sha256": "b" * 64, "drand_seed": "c" * 64,
        "archive_block_hash": "0x" + "d" * 64,
        "duplicate_review_sha256": canonical_sha256([]),
        "incumbent_hotkey": "incumbent", "baseline_package_sha256": "1" * 64,
        "reference_package_sha256": "1" * 64,
        "uploads": uploads, "sample": sample,
    }
    jobs = build_jobs(
        round_id="round-2", selected=entries, sample=sample,
        stage="screen", configuration_sha256=snapshot["configuration_sha256"],
    )
    return snapshot, ReplayInitialPlan(
        "round-2", "screen", hotkeys, (), tuple(entries), tuple(jobs),
    )


def _evaluation(**kwargs):
    hotkeys = kwargs["challenger_hotkeys"]
    scores = {"miner-a": 0.8, "miner-b": 0.7, "miner-c": 0.6, "miner-d": 0.4}
    return SimpleNamespace(
        pair_rows=tuple({
            "paper_id": "screen-paper", "challenger_hotkey": hotkey,
            "baseline_hotkey": "incumbent", "challenger_score": scores[hotkey],
            "baseline_score": 0.5, "infrastructure_fault": False,
            "details_public": {},
        } for hotkey in hotkeys),
        pipeline=SimpleNamespace(
            scores=[SimpleNamespace(miner_id=hotkey, score=scores[hotkey])
                    for hotkey in hotkeys]
                   + [SimpleNamespace(miner_id="incumbent", score=0.5)],
            silver_record=SimpleNamespace(model_dump=lambda **_: {"id": "silver-screen"}),
            diff_cases=[], adjudication_consensus=[],
        ),
    )


def test_replay_advances_from_own_screen_scores_and_enqueues_final(tmp_path):
    snapshot, plan = _screen_inputs()
    path = tmp_path / "replay.sqlite"
    store = ReplayStore(path)
    store.initialize(snapshot, plan)
    for _ in plan.jobs:
        job = store.claim_job("round-2", worker_id="replay", lease_seconds=60)
        assert job is not None
        store.complete_job(
            job["job_id"], worker_id="replay", lease_token=job["lease_token"],
            result={"status": "completed", "artifact": {"paper": {"paper_id": "screen-paper"}},
                    "inference_cost_usd": 0.1, "prompt_and_completion_tokens": 100},
            log_object_key="logs/replay",
        )
    assert score_ready_paper(
        "round-2", "screen", "screen-paper", backend=store,
        bronze_artifact={"paper": {"paper_id": "screen-paper"}},
        evaluate=_evaluation,
    )["status"] == "scored"
    result = advance_ready_stage("round-2", backend=ReplayStore(path))
    assert result["status"] == "started" and result["stage"] == "final"
    assert result["selected_hotkeys"] == ["miner-a", "miner-b", "miner-c"]
    restarted = ReplayStore(path)
    assert [row["stage"] for row in restarted.list_stages("round-2")] == ["screen", "final"]
    final = restarted.claim_job("round-2", worker_id="replay", lease_seconds=60)
    assert final["stage"] == "final" and final["hotkey"] == "incumbent"
    assert advance_ready_stage("round-2", backend=restarted)["status"] == "final_running"
