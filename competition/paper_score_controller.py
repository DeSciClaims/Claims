"""Freeze one stochastic paper adjudication, then publish its score rows."""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Protocol

from .gold_controller import contested_claims_for_paper
from .pair_evaluator import WeeklyPaperEvaluation
from .publication import canonical_sha256


class ScoringBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]: ...
    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None: ...
    def freeze_paper_evaluation(
        self, round_id: str, stage: str, paper_id: str,
        *, input_sha256: str, result_private: dict[str, Any],
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


def score_ready_paper(
    round_id: str, stage: str, paper_id: str, *, backend: ScoringBackend,
    bronze_artifact: Mapping[str, Any],
    evaluate: Callable[..., WeeklyPaperEvaluation],
) -> dict[str, Any]:
    """Return waiting or replay the immutable receipt into public score tables.

    ``evaluate`` is the frozen stage-specific policy: decision-model routing
    for early stages or the assigned two-pass judge configuration for the final.
    """
    round_record = backend.get_round(round_id)
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
        "configuration_sha256": round_record["configuration_sha256"],
        "source_sha256": paper["source_sha256"], "judge_id": paper["judge_id"],
        "bronze_sha256": canonical_sha256(bronze_artifact),
        "jobs": [{"job_id": job["job_id"], "hotkey": job["hotkey"],
                  "package_sha256": job["package_sha256"],
                  "result_sha256": canonical_sha256(job["result_private"])}
                 for job in sorted(jobs, key=lambda item: item["hotkey"])],
    })
    frozen = backend.get_paper_evaluation(round_id, stage, paper_id)
    if frozen is not None:
        if frozen["input_sha256"] != input_sha256:
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
            if scoring_stage in {"final", "week_one"}:
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
                "diff_cases": [row.model_dump(mode="json") for row in evaluation.pipeline.diff_cases],
                "adjudication_consensus": [row.model_dump(mode="json")
                                           for row in evaluation.pipeline.adjudication_consensus],
                "gold_candidates": gold_candidates,
            }
            if scoring_stage in {"final", "week_one"}:
                silver["miner_submissions"] = [{
                    "miner_id": submission.miner_id,
                    "candidates": [candidate.model_dump(mode="json")
                                   for candidate in submission.candidates],
                    "normal_findings": [finding.model_dump(mode="json")
                                        for finding in submission.normal_findings],
                } for submission in evaluation.pipeline.miner_submissions]
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
        result = {"scores": scores, "pairs": pairs, "silver": silver}
        frozen = backend.freeze_paper_evaluation(
            round_id, stage, paper_id, input_sha256=input_sha256, result_private=result,
        )
        result = frozen["result_private"]
    for item in result["scores"]:
        backend.record_score(item["job_id"], item["score"])
    for pair in result["pairs"]:
        backend.record_pair_score(round_id, stage, pair)
    return {"status": "scored", "paper_id": paper_id,
            "score_count": len(result["scores"]), "pair_count": len(result["pairs"])}
