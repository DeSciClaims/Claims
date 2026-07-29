from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Protocol

from .adjudication_models import AdjudicationContextBundle, AdjudicationConsensus
from .adjudication_runner import AdjudicationPass, run_adjudication_case
from .bronze_diff import compare_miner_to_bronze
from .orchestrator import MinerArtifactSubmission
from .record_projection import project_agent_artifact


class AdjudicationQueueBackend(Protocol):
    network: str

    def claim_adjudication_jobs(self, *, worker_id: str, limit: int = 1, lease_seconds: int = 900) -> list[dict[str, Any]]:
        ...

    def post_adjudication_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def post_adjudication_consensus(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def complete_adjudication_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        ...

    def post_adjudication_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class QueuedAdjudicationWorker:
    backend: AdjudicationQueueBackend
    worker_id: str
    adjudication_passes: list[AdjudicationPass]
    tiebreak_pass: AdjudicationPass | None = None
    direct_judge_confidence: float = 0.9
    lease_seconds: int = 900

    def run_once(self, *, limit: int = 1) -> list[dict[str, Any]]:
        jobs = self.backend.claim_adjudication_jobs(worker_id=self.worker_id, limit=limit, lease_seconds=self.lease_seconds)
        results: list[dict[str, Any]] = []
        for job in jobs:
            results.append(self._run_job(job))
        return results

    def _run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        try:
            context = _context_from_job(job)
            consensus = run_adjudication_case(
                context,
                passes=self.adjudication_passes,
                tiebreak_pass=self.tiebreak_pass,
                direct_judge_confidence=self.direct_judge_confidence,
            )
            self._post_consensus(job, consensus)
            result = {"case_id": consensus.case_id, "consensus": consensus.model_dump(mode="json")}
            return self.backend.complete_adjudication_job(
                job_id=job_id,
                worker_id=self.worker_id,
                status="completed",
                result=result,
            )
        except Exception as exc:
            return self.backend.complete_adjudication_job(
                job_id=job_id,
                worker_id=self.worker_id,
                status="failed",
                result={},
                error=f"{type(exc).__name__}: {exc}",
            )

    def _post_consensus(self, job: dict[str, Any], consensus: AdjudicationConsensus) -> None:
        for vote in consensus.votes:
            self.backend.post_adjudication_vote(
                {
                    "vote_id": f"{job['job_id']}_{vote.pass_id}",
                    "network": job.get("network") or self.backend.network,
                    "case_id": vote.case_id,
                    "run_id": job["run_id"],
                    "batch_id": job["batch_id"],
                    "paper_id": job["paper_id"],
                    "pass_id": vote.pass_id,
                    "adjudication_profile_id": vote.adjudication_profile_id,
                    "model_runtime_id": vote.model_runtime_id,
                    "candidate_order_seed": vote.case_id,
                    "disposition": vote.disposition,
                    "material_findings": vote.material_findings,
                    "cited_span_ids": vote.cited_span_ids,
                    "confidence": vote.confidence,
                    "rationale": vote.rationale,
                }
            )
        self.backend.post_adjudication_consensus(
            {
                "consensus_id": f"{job['job_id']}_consensus",
                "network": job.get("network") or self.backend.network,
                "case_id": consensus.case_id,
                "run_id": job["run_id"],
                "batch_id": job["batch_id"],
                "paper_id": job["paper_id"],
                "final_disposition": consensus.final_disposition,
                "final_confidence": consensus.final_confidence,
                "agreement_rate": consensus.agreement_rate,
                "route": consensus.route,
                "score_effects": [effect.model_dump(mode="json") for effect in consensus.score_effects],
            }
        )


def adjudication_job_payload(context: AdjudicationContextBundle) -> dict[str, Any]:
    return {"context": context.model_dump(mode="json")}


def build_adjudication_job_payloads_for_paper(
    *,
    run_id: str,
    batch_id: str,
    paper_id: str,
    bronze_artifact: dict,
    miner_artifacts: list[MinerArtifactSubmission],
    bronze_record_id: str | None = None,
    network: str = "testnet",
    source_context: str = "",
    priority: int = 0,
) -> list[dict[str, Any]]:
    bronze_candidates = project_agent_artifact(bronze_artifact, origin="bronze")
    miner_submissions = [
        (
            submission.miner_id,
            project_agent_artifact(submission.artifact, origin="miner", miner_id=submission.miner_id),
        )
        for submission in miner_artifacts
    ]
    candidate_pool = [*bronze_candidates, *[candidate for _miner_id, candidates in miner_submissions for candidate in candidates]]
    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidate_pool}
    jobs: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    for miner_id, miner_candidates in miner_submissions:
        for case in compare_miner_to_bronze(
            paper_id=paper_id,
            miner_id=miner_id,
            bronze_candidates=bronze_candidates,
            miner_candidates=miner_candidates,
        ):
            if case.case_id in seen_case_ids:
                continue
            seen_case_ids.add(case.case_id)
            context = AdjudicationContextBundle(
                case=case,
                candidates=[candidates_by_id[candidate_id] for candidate_id in case.candidate_ids if candidate_id in candidates_by_id],
                source_context=source_context,
                candidate_order_seed=case.case_id,
            )
            job_id = _adjudication_job_id(run_id=run_id, batch_id=batch_id, paper_id=paper_id, case_id=case.case_id)
            jobs.append(
                {
                    "job_id": job_id,
                    "network": network,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": paper_id,
                    "case_id": case.case_id,
                    "priority": priority,
                    "payload": {
                        **adjudication_job_payload(context),
                        "bronze_record_id": bronze_record_id,
                    },
                }
            )
    return jobs


def enqueue_adjudication_jobs(backend: AdjudicationQueueBackend, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [backend.post_adjudication_job(job) for job in jobs]


def completed_consensus_by_case(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if job.get("status") != "completed":
            continue
        result = job.get("result")
        if not isinstance(result, dict):
            continue
        consensus = result.get("consensus")
        if isinstance(consensus, dict) and result.get("case_id"):
            completed[str(result["case_id"])] = consensus
    return completed


def _context_from_job(job: dict[str, Any]) -> AdjudicationContextBundle:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("adjudication job payload must be an object")
    context = payload.get("context")
    if not isinstance(context, dict):
        raise ValueError("adjudication job payload missing context")
    return AdjudicationContextBundle.model_validate(context)


def _adjudication_job_id(*, run_id: str, batch_id: str, paper_id: str, case_id: str) -> str:
    digest = hashlib.sha256("|".join([run_id, batch_id, paper_id, case_id]).encode("utf-8")).hexdigest()[:16]
    return f"adjudication_job_{digest}"
