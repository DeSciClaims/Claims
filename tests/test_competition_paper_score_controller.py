import copy
from types import SimpleNamespace

import pytest

from competition.paper_score_controller import score_ready_paper


class Backend:
    def __init__(self):
        self.jobs = [
            self._job("reference", "c" * 64, "completed"),
            self._job("challenger", "d" * 64, "queued"),
        ]
        self.frozen = None
        self.scores = {}
        self.pairs = {}

    @staticmethod
    def _job(hotkey, digest, status):
        return {
            "job_id": hotkey + "-job", "hotkey": hotkey,
            "package_sha256": digest, "configuration_sha256": "b" * 64,
            "stage": "week_one", "paper_id": "paper-1", "status": status,
            "result_private": {"status": "completed", "artifact": {"paper": {"paper_id": "paper-1"}},
                               "inference_cost_usd": 0.12},
        }

    def get_round(self, _):
        return {"configuration_sha256": "b" * 64}

    def list_sample(self, _):
        return [{"stage": "week_one", "paper_id": "paper-1", "judge_id": "J1",
                 "source_sha256": "e" * 64}]

    def list_stages(self, _):
        return [{"stage": "week_one", "entries": [
            {"hotkey": "reference", "package_sha256": "c" * 64, "role": "reference"},
            {"hotkey": "challenger", "package_sha256": "d" * 64, "role": "challenger"},
        ]}]

    def list_paper_jobs(self, *_):
        return self.jobs

    def get_paper_evaluation(self, *_):
        return self.frozen

    def freeze_paper_evaluation(self, _, stage, paper_id, *, input_sha256, result_private, **_generation):
        assert stage == "week_one" and paper_id == "paper-1"
        self.frozen = {"input_sha256": input_sha256, "result_private": result_private}
        return self.frozen

    def record_score(self, job_id, score):
        existing = self.scores.setdefault(job_id, score)
        assert existing == score
        return score

    def record_pair_score(self, _, stage, pair):
        assert stage == "week_one"
        existing = self.pairs.setdefault(pair["challenger_hotkey"], pair)
        assert existing == pair
        return pair


def test_revision_aware_scoring_carries_configuration_fence_to_every_write():
    class RevisionBackend(Backend):
        supports_rules_revisions = True

        def freeze_paper_evaluation(self, *args, expected_configuration_sha256,
                                    expected_rules_revision, **kwargs):
            assert expected_configuration_sha256 == "b" * 64
            assert expected_rules_revision == 1
            self.freeze_fence = expected_configuration_sha256
            return super().freeze_paper_evaluation(*args, **kwargs)

        def record_pair_score(self, rid, stage, pair):
            assert pair["expected_configuration_sha256"] == "b" * 64
            assert pair["expected_rules_revision"] == 1
            self.pair_fence = pair["expected_configuration_sha256"]
            return super().record_pair_score(rid, stage,
                {key: value for key, value in pair.items() if not key.startswith("expected_")})

        def record_score(self, job_id, score):
            assert score["expected_configuration_sha256"] == "b" * 64
            assert score["expected_rules_revision"] == 1
            return super().record_score(job_id,
                {key: value for key, value in score.items() if not key.startswith("expected_")})

    backend = RevisionBackend()
    backend.jobs[1]["status"] = "completed"
    assert score_ready_paper("round-1", "week_one", "paper-1", backend=backend,
                            bronze_artifact={}, evaluate=_evaluation)["status"] == "scored"
    assert backend.freeze_fence == backend.pair_fence == "b" * 64


@pytest.mark.parametrize("options", [
    {"expected_configuration_sha256": "old"},
    {"expected_rules_revision": 2},
])
def test_a_stale_evaluator_is_rejected_before_any_paper_work(options):
    backend = Backend()
    with pytest.raises(ValueError, match="stale"):
        score_ready_paper("round-1", "week_one", "paper-1", backend=backend,
                          bronze_artifact={}, evaluate=lambda **_: pytest.fail("must not judge"),
                          **options)
    assert backend.frozen is None and not backend.scores and not backend.pairs


def _evaluation(**kwargs):
    assert kwargs["paper_id"] == "paper-1"
    assert kwargs["judge_id"] == "J1"
    return SimpleNamespace(
        judge_usage_events=({"model": "example/judge", "pass_id": "J1-first",
                             "status": "success", "cost_kind": "actual",
                             "cost_usd": 0.02},),
        pair_rows=({
            "paper_id": "paper-1", "challenger_hotkey": "challenger",
            "baseline_hotkey": "reference", "challenger_score": 0.7,
            "baseline_score": 0.6, "infrastructure_fault": False,
            "details_public": {},
        },),
        pipeline=SimpleNamespace(
            paper_id="paper-1", miner_submissions=[],
            scores=[SimpleNamespace(miner_id="challenger", score=0.7),
                    SimpleNamespace(miner_id="reference", score=0.6)],
            silver_record=SimpleNamespace(model_dump=lambda **_: {"id": "silver-1"}, silver_units=[]),
            diff_cases=[], adjudication_consensus=[],
        ),
    )


def test_paper_waits_for_complete_grid_then_replays_frozen_adjudication():
    backend = Backend()
    call = lambda evaluator: score_ready_paper(
        "round-1", "week_one", "paper-1", backend=backend,
        bronze_artifact={"paper": {"paper_id": "paper-1"}}, evaluate=evaluator,
    )
    assert call(_evaluation)["status"] == "waiting_for_jobs"
    backend.jobs[1]["status"] = "completed"
    assert call(_evaluation) == {"status": "scored", "paper_id": "paper-1",
                                  "score_count": 2, "pair_count": 1}
    assert backend.scores["challenger-job"]["score"] == 0.7
    assert backend.scores["challenger-job"]["details_public"]["inference_cost_usd"] == 0.12
    assert backend.pairs["challenger"]["baseline_score"] == 0.6
    assert backend.frozen["result_private"]["judge_usage"][0]["cost_usd"] == 0.02
    backend.frozen["carried_forward"] = {}  # PostgreSQL's unamended-receipt default.
    assert call(lambda **_: pytest.fail("adjudication must not rerun"))["status"] == "scored"
    backend.jobs[1]["result_private"] = {"status": "invalid_output", "inference_cost_usd": 0.12}
    with pytest.raises(ValueError, match="inputs changed"):
        call(_evaluation)


def test_infrastructure_fault_skips_adjudication_and_scores_everyone_zero():
    backend = Backend()
    for job in backend.jobs:
        job["status"] = "completed"
    backend.jobs[1]["result_private"] = {"status": "infrastructure_fault", "inference_cost_usd": 0.03}
    result = score_ready_paper(
        "round-1", "week_one", "paper-1", backend=backend,
        bronze_artifact={}, evaluate=lambda **_: pytest.fail("must not adjudicate"),
    )
    assert result["status"] == "scored"
    assert {row["fault_kind"] for row in backend.scores.values()} == {"infrastructure"}
    assert all(row["score"] == 0 for row in backend.scores.values())
    assert backend.pairs["challenger"]["infrastructure_fault"] is True


def test_reference_rerun_uses_original_stage_scoring_policy():
    backend = Backend()
    for job in backend.jobs:
        job["status"] = "completed"
        job["stage"] = "rerun"
    backend.list_sample = lambda _: [{"stage": "rerun", "paper_id": "paper-1",
                                      "judge_id": "J1", "source_sha256": "e" * 64}]
    backend.list_stages = lambda _: [{"stage": "rerun", "source_stage": "screen",
                                      "entries": Backend.list_stages(backend, "round-1")[0]["entries"]}]
    backend.freeze_paper_evaluation = lambda _, stage, paper_id, *, input_sha256, result_private: {
        "input_sha256": input_sha256, "result_private": result_private,
    }
    backend.record_pair_score = lambda _, stage, pair: pair

    def evaluate(**kwargs):
        assert kwargs["stage"] == "screen"
        return _evaluation(**kwargs)

    assert score_ready_paper(
        "round-1", "rerun", "paper-1", backend=backend,
        bronze_artifact={"paper": {"paper_id": "paper-1"}}, evaluate=evaluate,
    )["status"] == "scored"


def test_resume_publishes_saved_paper_scores_without_rejudging_after_amendment():
    backend = Backend()
    for job in backend.jobs:
        job["status"] = "completed"
    score_ready_paper("round-1", "week_one", "paper-1", backend=backend,
                      bronze_artifact={}, evaluate=_evaluation)
    original = copy.deepcopy(backend.frozen)
    mapping = {job["job_id"]: job["job_id"] + "-r2" for job in backend.jobs}
    for job in backend.jobs:
        job["job_id"] = mapping[job["job_id"]]
        job["configuration_sha256"] = "f" * 64
        job["result_private"]["reused_execution"] = {"new_inference_calls": 0}
    backend.get_round = lambda _: {"configuration_sha256": "f" * 64,
                                   "rules_revision": 2, "rules_revision_sha256": "a" * 64}
    backend.frozen["carried_forward"] = {
        "to_revision": 2, "from_revision": 1, "configuration_sha256": "b" * 64,
        "input_sha256": original["input_sha256"], "job_id_map": mapping,
    }
    backend.scores, backend.pairs = {}, {}
    backend.supports_rules_revisions = True

    def freeze(_, stage, paper_id, *, input_sha256, result_private,
               expected_configuration_sha256, expected_rules_revision):
        assert input_sha256 == original["input_sha256"]
        assert result_private == original["result_private"]
        assert expected_configuration_sha256 == "f" * 64 and expected_rules_revision == 2
        return backend.frozen

    backend.freeze_paper_evaluation = freeze
    for _ in range(2):
        result = score_ready_paper("round-1", "week_one", "paper-1", backend=backend,
            bronze_artifact={}, evaluate=lambda **_: pytest.fail("saved paper must not be judged again"))
        assert result["status"] == "scored"
    assert set(backend.scores) == set(mapping.values())
    assert backend.scores["challenger-job-r2"]["score"] == 0.7
    assert backend.scores["challenger-job-r2"]["expected_rules_revision"] == 2
    assert backend.frozen["result_private"] == original["result_private"]

    backend.frozen["carried_forward"]["to_revision"] = 1
    with pytest.raises(ValueError, match="revision provenance"):
        score_ready_paper("round-1", "week_one", "paper-1", backend=backend,
            bronze_artifact={}, evaluate=lambda **_: pytest.fail("must not judge"))


def test_independent_replay_binds_saved_result_to_the_actual_judging_revision():
    backend = Backend()
    for job in backend.jobs:
        job["status"] = "completed"
    backend.get_round = lambda _: {"configuration_sha256": "b" * 64, "rules_revision": 2}
    origin = {"rules_revision": 1, "configuration_sha256": "a" * 64}
    args = dict(backend=backend, bronze_artifact={}, evaluate=_evaluation)
    score_ready_paper("round-1", "week_one", "paper-1", **args, scoring_provenance=origin)
    assert backend.frozen["result_private"]["scoring_provenance"] == origin
    with pytest.raises(ValueError, match="inputs changed"):
        score_ready_paper("round-1", "week_one", "paper-1", **args,
                          scoring_provenance={"rules_revision": 2, "configuration_sha256": "b" * 64})
