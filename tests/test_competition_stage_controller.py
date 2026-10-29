import pytest

from competition.failure_policy import ReferenceFallbackRequired
from competition.stage_controller import advance_ready_stage, start_initial_stage


class Backend:
    def __init__(self):
        self.round = {
            "incumbent_hotkey": "incumbent",
            "baseline_package_sha256": "a" * 64,
            "configuration_sha256": "d" * 64,
        }
        self.sample = [
            {"stage": "screen", "paper_id": "paper-1"},
            {"stage": "screen", "paper_id": "paper-2"},
            {"stage": "final", "paper_id": "paper-3"},
        ]
        self.ledger = []
        self.enqueued = []
        self.pairs = []
        self.submissions = [
            {"hotkey": "miner-a", "commit_block": 98, "status": "accepted"},
            {"hotkey": "miner-b", "commit_block": 99, "status": "accepted"},
            {"hotkey": "miner-c", "commit_block": 97, "status": "accepted"},
            {"hotkey": "miner-d", "commit_block": 96, "status": "accepted"},
        ]

    def get_round(self, round_id):
        return self.round

    def list_sample(self, round_id):
        return self.sample

    def list_stages(self, round_id):
        return self.ledger

    def list_stage_pairs(self, round_id, stage):
        return self.pairs

    def list_submissions(self, round_id):
        return self.submissions

    def list_paper_jobs(self, round_id, stage, paper_id):
        return [{"hotkey": "incumbent", "status": "completed",
                 "result_private": {"status": "completed"}}]

    def start_stage(self, round_id, stage, entries):
        self.ledger.append({"stage": stage, "entries": entries})

    def enqueue_jobs(self, round_id, jobs):
        self.enqueued.extend(jobs)


def test_stage_controller_waits_for_full_grid_and_advances_one_per_team():
    backend = Backend()
    result = start_initial_stage(
        "week2", backend=backend,
        baseline={"hotkey": "incumbent", "package_sha256": "a" * 64, "team_id": "king-team"},
        challengers=[
            {"hotkey": "miner-b", "package_sha256": "c" * 64,
             "team_id": "one-team", "commit_block": 99},
            {"hotkey": "miner-a", "package_sha256": "b" * 64,
             "team_id": "one-team", "commit_block": 98},
            {"hotkey": "miner-c", "package_sha256": "e" * 64,
             "team_id": "team-c", "commit_block": 97},
            {"hotkey": "miner-d", "package_sha256": "f" * 64,
             "team_id": "team-d", "commit_block": 96},
        ],
    )
    assert backend.ledger[0]["stage"] == "screen"
    assert result["status"] == "started"
    assert [row["hotkey"] for row in backend.ledger[0]["entries"]] == [
        "incumbent", "miner-d", "miner-c", "miner-a", "miner-b",
    ]
    assert len(backend.enqueued) == 10
    assert {job["paper_id"] for job in backend.enqueued} == {"paper-1", "paper-2"}
    assert advance_ready_stage("week2", backend=backend)["status"] == "waiting_for_scores"
    values = {
        "miner-a": (0.9, 0.8), "miner-b": (0.5, 0.1),
        "miner-c": (0.45, 0.4), "miner-d": (0.3, 0.4),
    }
    for paper_id in ("paper-1", "paper-2"):
        for hotkey, (challenger, baseline) in values.items():
            digest = {"miner-a": "b", "miner-b": "c", "miner-c": "e", "miner-d": "f"}[hotkey] * 64
            backend.pairs.append({
                "stage": "screen", "paper_id": paper_id,
                "challenger_hotkey": hotkey, "challenger_package_sha256": digest,
                "baseline_hotkey": "incumbent", "baseline_package_sha256": "a" * 64,
                "challenger_score": challenger if paper_id == "paper-1" else 0,
                "baseline_score": baseline if paper_id == "paper-1" else 0,
                "infrastructure_fault": paper_id == "paper-2",
            })
    result = advance_ready_stage("week2", backend=backend)
    assert result == {"stage": "final", "status": "started",
                      "selected_hotkeys": ["miner-b", "miner-c", "miner-d"]}
    assert [row["hotkey"] for row in backend.ledger[1]["entries"]] == [
        "incumbent", "miner-b", "miner-c", "miner-d",
    ]
    assert backend.ledger[1]["entries"][1]["paired_gain"] == 0.4
    assert {job["paper_id"] for job in backend.enqueued[-4:]} == {"paper-3"}


def test_screen_does_not_advance_against_failed_incumbent():
    backend = Backend()
    backend.ledger = [{"stage": "screen", "entries": [
        {"hotkey": "incumbent", "package_sha256": "a" * 64,
         "team_id": "king-team", "role": "incumbent", "position": 0},
        {"hotkey": "miner-a", "package_sha256": "b" * 64,
         "team_id": "one-team", "role": "challenger", "position": 1},
    ]}]
    backend.pairs = [{
        "stage": "screen", "paper_id": paper_id,
        "challenger_hotkey": "miner-a", "challenger_package_sha256": "b" * 64,
        "baseline_hotkey": "incumbent", "baseline_package_sha256": "a" * 64,
        "challenger_score": 0.5, "baseline_score": 0.0,
        "infrastructure_fault": False,
    } for paper_id in ("paper-1", "paper-2")]
    backend.list_paper_jobs = lambda _round_id, _stage, paper_id: [{
        "hotkey": "incumbent", "status": "completed",
        "result_private": {"status": "package_failed" if paper_id == "paper-1" else "completed"},
    }]
    with pytest.raises(ReferenceFallbackRequired) as exc:
        advance_ready_stage("week2", backend=backend)
    assert (exc.value.summary.failed_papers, exc.value.summary.eligible_papers) == (1, 2)
    assert len(backend.ledger) == 1


def test_versioned_two_stage_smoke_advances_top_three_directly_to_final():
    backend = Backend()
    backend.round.update({
        "network": "testnet", "deadline_at": "2026-11-04T12:00:00+00:00",
        "rules_public": {"policy": {
            "schedule": {"mode": "custom", "reveal_delay_blocks": 1,
                         "gold_close_at": "2026-11-04T13:00:00+00:00",
                         "publish_at": "2026-11-04T13:05:00+00:00",
                         "production_activate_at": "2026-11-04T13:10:00+00:00"},
            "evaluation": {
                "schema_version": "claims_competition_evaluation_v1",
                "mode": "testnet_smoke", "required_challengers": 5,
                "stages": [
                    {"name": "prescreen", "papers": 2, "advance_top": 3,
                     "max_per_team": 1},
                    {"name": "final", "papers": 5},
                ],
                "gold_known_answers": 1,
            },
        }},
    })
    backend.sample = [
        {"stage": "prescreen", "paper_id": f"early-{i}"} for i in range(2)
    ] + [{"stage": "final", "paper_id": f"final-{i}"} for i in range(5)]
    backend.submissions = [
        {"hotkey": f"miner-{i}", "commit_block": 100 + i, "status": "accepted"}
        for i in range(5)
    ]
    entrants = [
        {"hotkey": f"miner-{i}", "package_sha256": f"{i + 1:064x}",
         "team_id": f"team-{i}", "commit_block": 100 + i}
        for i in range(5)
    ]
    result = start_initial_stage(
        "staged", backend=backend,
        baseline={"hotkey": "incumbent", "package_sha256": "a" * 64,
                  "team_id": "king-team"}, challengers=entrants,
    )
    assert result["stage"] == "prescreen"
    assert len(backend.enqueued) == 12
    backend.pairs = [
        {"stage": "prescreen", "paper_id": f"early-{paper}",
         "challenger_hotkey": f"miner-{i}",
         "challenger_package_sha256": f"{i + 1:064x}",
         "baseline_hotkey": "incumbent", "baseline_package_sha256": "a" * 64,
         "challenger_score": i / 10, "baseline_score": 0.0,
         "infrastructure_fault": False}
        for paper in range(2) for i in range(5)
    ]
    result = advance_ready_stage("staged", backend=backend)
    assert result["stage"] == "final"
    assert result["selected_hotkeys"] == ["miner-4", "miner-3", "miner-2"]
    assert sum(job["stage"] == "final" for job in backend.enqueued) == 20


def test_promotion_waits_for_identity_review_and_uses_linked_teams():
    backend = Backend()
    start_initial_stage(
        "round",
        backend=backend,
        baseline={
            "hotkey": "incumbent",
            "package_sha256": "a" * 64,
            "team_id": "king-team",
        },
        challengers=[
            {
                "hotkey": "miner-" + name,
                "package_sha256": digest * 64,
                "team_id": "team-" + name,
                "commit_block": 90 + index,
            }
            for index, (name, digest) in enumerate(
                [("a", "b"), ("b", "c"), ("c", "e"), ("d", "f")]
            )
        ],
    )
    backend.round["rules_public"] = {"policy": {"team_identity": "linked"}}
    for pid in ("paper-1", "paper-2"):
        for index, (name, digest) in enumerate(
            [("a", "b"), ("b", "c"), ("c", "e"), ("d", "f")]
        ):
            backend.pairs.append(
                {
                    "stage": "screen",
                    "paper_id": pid,
                    "challenger_hotkey": "miner-" + name,
                    "challenger_package_sha256": digest * 64,
                    "baseline_hotkey": "incumbent",
                    "baseline_package_sha256": "a" * 64,
                    "challenger_score": 0.9 - index * 0.1,
                    "baseline_score": 0.3,
                    "infrastructure_fault": False,
                }
            )
    result = advance_ready_stage(
        "round",
        backend=backend,
        identity_preparer=lambda _: {"status": "waiting_for_team_review"},
    )
    assert result["status"] == "waiting_for_team_review"
    assert len(backend.ledger) == 1
    result = advance_ready_stage(
        "round",
        backend=backend,
        identity_preparer=lambda _: {
            "team_by_hotkey": {
                "incumbent": "king-team",
                "miner-a": "linked-team",
                "miner-b": "linked-team",
                "miner-c": "team-c",
                "miner-d": "team-d",
            }
        },
    )
    assert result["selected_hotkeys"] == ["miner-a", "miner-c", "miner-d"]
    assert backend.ledger[-1]["entries"][1]["team_id"] == "linked-team"
