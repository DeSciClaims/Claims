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

    def start_stage(self, round_id, stage, entries):
        self.ledger.append({"stage": stage, "entries": entries})

    def enqueue_jobs(self, round_id, jobs):
        self.enqueued.extend(jobs)


def test_stage_controller_waits_for_full_grid_and_advances_one_per_team():
    backend = Backend()
    start_initial_stage(
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
