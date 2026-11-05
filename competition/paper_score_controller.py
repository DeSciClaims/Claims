"""Freeze one stochastic paper adjudication, then publish its score rows."""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Mapping, Protocol

from .gold_controller import contested_claims_for_paper
from .pair_evaluator import WeeklyPaperEvaluation
from .publication import canonical_sha256
from .review_policy import uses_miner_review


class ScoringBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]: ...
    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None: ...
    def freeze_paper_evaluation(
        self, round_id: str, stage: str, paper_id: str,
        *, input_sha256: str, result_private: dict[str, Any],
        expected_configuration_sha256: str | None = None,
        expected_rules_revision: int | None = None,
    ) -> dict[str, Any]: ...
    def record_score(self, job_id: str, score: dict[str, Any]) -> dict[str, Any]: ...
    def record_pair_score(self, round_id: str, stage: str, payload: dict[str, Any]) -> dict[str, Any]: ...


def _status_fault(status: str) -> str:
    if status == "completed":
        return "none"
    if status == "infrastructure_fault":
        return "infrastructure"
    if status == "cap_exceeded":
        return "cap"
    if status in {"build_failed", "package_failed", "invalid_output", "timeout",
                  "security_violation"}:
        return "package"
    raise ValueError("paper job has an unknown worker status")


def _continued_score_jobs(frozen: Mapping[str, Any], round_record: Mapping[str, Any],
                          jobs: list[dict[str, Any]]) -> dict[str, str] | None:
    """Accept only the backend's explicit carry-forward of an old checkpoint.

    The original receipt stays immutable. Its score rows refer to original job
    IDs, while extraction jobs are rebound to the newly approved generation.
    """
    carried = frozen.get("carried_forward")
    if carried is None or carried == {}:
        return None
    revision = round_record.get("rules_revision", 1)
    if (not isinstance(carried, dict)
        or type(carried.get("from_revision")) is not int
        or not 1 <= carried["from_revision"] < revision
        or carried.get("to_revision") != revision
        or carried.get("input_sha256") != frozen.get("input_sha256")
        or not re.fullmatch(r"[0-9a-f]{64}", str(carried.get("configuration_sha256", "")))
        or not isinstance(carried.get("job_id_map"), dict)):
        raise ValueError("preserved paper checkpoint has invalid revision provenance")
    mapping = carried["job_id_map"]
    score_ids = [item["job_id"] for item in frozen["result_private"]["scores"]]
    if (len(set(score_ids)) != len(score_ids) or set(mapping) != set(score_ids)
        or set(mapping.values()) != {job["job_id"] for job in jobs}
        or len(set(mapping.values())) != len(jobs)):
        raise ValueError("preserved paper checkpoint differs from current execution grid")
    return mapping


def score_ready_paper(
    round_id: str, stage: str, paper_id: str, *, backend: ScoringBackend,
    bronze_artifact: Mapping[str, Any],
    evaluate: Callable[..., WeeklyPaperEvaluation],
    expected_configuration_sha256: str | None = None,
    expected_rules_revision: int | None = None,
    scoring_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return waiting or replay the immutable receipt into public score tables.

    ``evaluate`` is the frozen stage-specific policy: decision-model routing
    for early stages or the assigned two-pass judge configuration for the final.
    """
    round_record = backend.get_round(round_id)
    if scoring_provenance is not None:
        if (set(scoring_provenance) != {"rules_revision", "configuration_sha256"}
            or type(scoring_provenance.get("rules_revision")) is not int
            or not 1 <= scoring_provenance["rules_revision"] <= round_record.get("rules_revision", 1)
            or not re.fullmatch(r"[0-9a-f]{64}", str(scoring_provenance.get("configuration_sha256", "")))):
            raise ValueError("paper scoring provenance is invalid")
        scoring_provenance = dict(scoring_provenance)
    if (expected_configuration_sha256 is not None
        and expected_configuration_sha256 != round_record["configuration_sha256"]):
        raise ValueError("paper evaluator belongs to a stale configuration")
    if (expected_rules_revision is not None
        and (type(expected_rules_revision) is not int or expected_rules_revision < 1
             or expected_rules_revision != round_record.get("rules_revision", 1))):
        raise ValueError("paper evaluator belongs to a stale rules revision")
    stage_receipts = backend.list_stages(round_id)
    receipt = next((row for row in stage_receipts if row["stage"] == stage), None)
    if receipt is None:
        raise ValueError("stage has no frozen entrants")
    scoring_stage = receipt.get("source_stage") if stage == "rerun" else stage
    if scoring_stage not in {"prescreen", "screen", "final", "week_one"}:
        raise ValueError("paper evaluation has an invalid source stage")
    paper = next((row for row in backend.list_sample(round_id)
                  if row["stage"] == stage and row["paper_id"] == paper_id), None)
    if paper is None:
        raise ValueError("paper is outside the released stage sample")
    entries = receipt["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    challengers = [row for row in entries if row["role"] == "challenger"]
    if len(baseline) != 1 or not challengers:
        raise ValueError("stage requires one baseline and challengers")
    expected = {row["hotkey"]: row["package_sha256"] for row in entries}
    if len(expected) != len(entries):
        raise ValueError("stage has duplicate entrant hotkeys")
    jobs = backend.list_paper_jobs(round_id, stage, paper_id)
    by_hotkey = {job["hotkey"]: job for job in jobs}
    if len(by_hotkey) != len(jobs) or set(by_hotkey) != set(expected):
        return {"status": "waiting_for_jobs", "complete": 0, "expected": len(expected)}
    if any(job["package_sha256"] != expected[hotkey]
           or job["configuration_sha256"] != round_record["configuration_sha256"]
           or job["stage"] != stage or job["paper_id"] != paper_id
           for hotkey, job in by_hotkey.items()):
        raise ValueError("paper jobs differ from frozen entrants and configuration")
    complete_count = sum(job["status"] == "completed" for job in jobs)
    if complete_count != len(jobs):
        return {"status": "waiting_for_jobs", "complete": complete_count, "expected": len(jobs)}
    for job in jobs:
        result_private = job.get("result_private")
        if not isinstance(result_private, dict):
            raise ValueError("completed paper job has no private result")
        cost = result_private.get("inference_cost_usd")
        if (isinstance(cost, bool) or not isinstance(cost, int | float)
            or not math.isfinite(cost) or cost < 0):
            raise ValueError("completed paper job has no valid gateway cost")
    input_sha256 = canonical_sha256({
        "round_id": round_id, "stage": stage, "paper_id": paper_id,
        **({"rules_revision": int(round_record["rules_revision"]),
            "rules_revision_sha256": round_record.get("rules_revision_sha256")}
           if int(round_record.get("rules_revision", 1)) > 1 else {}),
        "configuration_sha256": round_record["configuration_sha256"],
        **({"scoring_provenance": scoring_provenance} if scoring_provenance is not None else {}),
        "source_sha256": paper["source_sha256"], "judge_id": paper["judge_id"],
        "bronze_sha256": canonical_sha256(bronze_artifact),
        "jobs": [{"job_id": job["job_id"], "hotkey": job["hotkey"],
                  "package_sha256": job["package_sha256"],
                  "result_sha256": canonical_sha256(job["result_private"])}
                 for job in sorted(jobs, key=lambda item: item["hotkey"])],
    })
    frozen = backend.get_paper_evaluation(round_id, stage, paper_id)
    continued_jobs = None
    if frozen is not None:
        continued_jobs = _continued_score_jobs(frozen, round_record, jobs)
        if continued_jobs is not None:
            input_sha256 = frozen["input_sha256"]
        elif frozen["input_sha256"] != input_sha256:
            raise ValueError("frozen paper evaluation inputs changed")
        result = frozen["result_private"]
    else:
        outcomes = {hotkey: job["result_private"] for hotkey, job in by_hotkey.items()}
        faults = {hotkey: _status_fault(outcome["status"]) for hotkey, outcome in outcomes.items()}
        infrastructure_fault = "infrastructure" in faults.values()
        if infrastructure_fault:
            scores_by_hotkey: dict[str, float] = {}
            pairs = [{
                "paper_id": paper_id, "challenger_hotkey": row["hotkey"],
                "baseline_hotkey": baseline[0]["hotkey"],
                "challenger_score": 0.0, "baseline_score": 0.0,
                "infrastructure_fault": True,
                "details_public": {"reason": "infrastructure_fault"},
            } for row in challengers]
            silver = None
        else:
            artifacts = {hotkey: outcome.get("artifact") if faults[hotkey] == "none" else None
                         for hotkey, outcome in outcomes.items()}
            evaluation = evaluate(
                paper_id=paper_id, stage=scoring_stage, judge_id=paper["judge_id"],
                bronze_artifact=dict(bronze_artifact), artifacts_by_hotkey=artifacts,
                baseline_hotkey=baseline[0]["hotkey"],
                challenger_hotkeys=[row["hotkey"] for row in challengers],
            )
            pairs = list(evaluation.pair_rows)
            scores_by_hotkey = {row.miner_id: row.score for row in evaluation.pipeline.scores}
            gold_candidates = []
            if scoring_stage in {"final", "week_one"} and uses_miner_review(round_record):
                gold_candidates = [{
                    "case": {
                        "case_id": claim.case.case_id,
                        "paper_id": claim.case.paper_id,
                        "challenger_hotkey": claim.case.challenger_hotkey,
                        "incumbent_hotkey": claim.case.incumbent_hotkey,
                        "excluded_team_ids": sorted(claim.case.excluded_team_ids),
                    },
                    "case_public": dict(claim.case_public),
                    "answer_private": dict(claim.answer_private),
                } for claim in contested_claims_for_paper(round_id, evaluation, entries)]
            silver = {
                "silver_record": evaluation.pipeline.silver_record.model_dump(mode="json"),
                "bronze_candidates": [row.model_dump(mode="json") for row in getattr(evaluation.pipeline, "bronze_candidates", [])],
                "diff_cases": [row.model_dump(mode="json") for row in evaluation.pipeline.diff_cases],
                "adjudication_consensus": [row.model_dump(mode="json")
                                           for row in evaluation.pipeline.adjudication_consensus],
                "gold_candidates": gold_candidates,
                "adjudication_decisions": [row.model_dump(mode="json")
                    for row in getattr(evaluation.pipeline, "adjudication_decisions", [])],
                "miner_submissions": [{
                    "miner_id": submission.miner_id,
                    "candidates": [candidate.model_dump(mode="json") for candidate in submission.candidates],
                    "normal_findings": [finding.model_dump(mode="json") for finding in submission.normal_findings],
                } for submission in getattr(evaluation.pipeline, "miner_submissions", [])],
            }
        if (len(pairs) != len(challengers)
            or {row["challenger_hotkey"] for row in pairs} != {row["hotkey"] for row in challengers}
            or any(row["paper_id"] != paper_id
                   or row["baseline_hotkey"] != baseline[0]["hotkey"]
                   or bool(row["infrastructure_fault"]) != infrastructure_fault
                   or not 0 <= row["challenger_score"] <= 1
                   or not 0 <= row["baseline_score"] <= 1
                   for row in pairs)):
            raise ValueError("adjudication did not return one pair for every challenger")
        scores = []
        for hotkey, job in sorted(by_hotkey.items()):
            fault = "infrastructure" if infrastructure_fault else faults[hotkey]
            if fault == "none" and hotkey not in scores_by_hotkey:
                raise ValueError("adjudication did not score a completed package")
            score = 0.0 if fault != "none" else scores_by_hotkey[hotkey]
            if not 0 <= score <= 1:
                raise ValueError("adjudication score is outside [0,1]")
            scores.append({"job_id": job["job_id"], "score": {
                "score": score, "fault_kind": fault,
                "details_public": {
                    "worker_status": outcomes[hotkey]["status"],
                    "total_tokens": outcomes[hotkey].get("prompt_and_completion_tokens"),
                    "inference_cost_usd": outcomes[hotkey].get("inference_cost_usd"),
                },
            }})
        result = {"scores": scores, "pairs": pairs, "silver": silver,
                  **({"scoring_provenance": scoring_provenance} if scoring_provenance is not None else {}),
                  "resources": (getattr(evaluation, "resources", None)
                                if not infrastructure_fault else None),
                  "judge_usage": (list(getattr(evaluation, "judge_usage_events", None))
                                  if not infrastructure_fault
                                  and getattr(evaluation, "judge_usage_events", None) is not None else None)}
    generation = ({"expected_configuration_sha256": expected_configuration_sha256 or round_record["configuration_sha256"],
                   "expected_rules_revision": expected_rules_revision or round_record.get("rules_revision", 1)}
                  if getattr(backend, "supports_rules_revisions", False)
                  or round_record.get("rules_revision", 1) > 1 else {})
    frozen = backend.freeze_paper_evaluation(
        round_id, stage, paper_id, input_sha256=input_sha256, result_private=result,
        **generation,
    )
    result = frozen["result_private"]
    continued_jobs = _continued_score_jobs(frozen, round_record, jobs) or continued_jobs
    for item in result["scores"]:
        job_id = continued_jobs[item["job_id"]] if continued_jobs is not None else item["job_id"]
        backend.record_score(job_id, {**item["score"], **generation})
    for pair in result["pairs"]:
        backend.record_pair_score(round_id, stage, {**pair, **generation})
    return {"status": "scored", "paper_id": paper_id,
            "score_count": len(result["scores"]), "pair_count": len(result["pairs"])}
