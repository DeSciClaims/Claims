"""One resumable tick of the trusted weekly competition controller.

An external timer calls ``tick_round`` repeatedly. All durable transitions
live in the backend, so a controller restart rereads the round and resumes.
Miner code executes only in disposable workers. The trusted judge adapter
holds its own inference credential outside miner execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol

from .copy_audit import audit_final_output_copies
from .drand import BlockClock
from .failure_policy import ReferenceFallbackRequired, incumbent_failure_summary
from .final_controller import select_ready_final
from .gold_challenges import build_known_answer_challenges
from .gold_controller import (
    audit_closed_known_answers, frozen_gold_candidates, prepare_frozen_gold_review,
    tally_closed_gold_review,
)
from .intake_lock import ArchiveReader, CompetitionBackend
from .paper_score_controller import score_ready_paper
from .result_controller import publish_selected_round
from .round_controller import lock_ready_round, reveal_selected_sample, start_revealed_round
from .round_schedule import schedule_for_round
from .smoke_profile import smoke_profile
from .round_evaluation import round_evaluation
from .sample_release import PaperRecord
from .stage_controller import advance_ready_stage, start_failed_incumbent_reference_rerun
from .worker_security_audit import audit_final_worker_security


@dataclass(frozen=True)
class FinalAudit:
    code_passed_by_hotkey: Mapping[str, bool]
    qualified_by_hotkey: Mapping[str, bool]
    uid_by_hotkey: Mapping[str, int]
    production_team_id: str | None
    code_failure_counts_by_hotkey: Mapping[str, int]
    code_evidence_sha256: str


class TrustedRoundInputs(Protocol):
    archive: ArchiveReader
    chain: BlockClock
    netuid: int
    signer: Any
    operator_hotkey: str

    def package_for_submission(self, submission: Mapping[str, Any]) -> bytes: ...
    def papers_for_round(self, round_id: str) -> list[PaperRecord]: ...
    def baseline_for_round(self, round_id: str) -> Mapping[str, Any]: ...
    def reference_for_round(self, round_id: str) -> Mapping[str, Any]: ...
    def incumbent_package_for_round(self, round_id: str) -> bytes | None: ...
    def linked_pairs_for_round(self, round_id: str) -> list[tuple[str, str, str]]: ...
    def weekly_budget_for_round(self, round_id: str) -> float: ...
    def bronze_for_paper(self, stage: str, paper_id: str) -> Mapping[str, Any]: ...
    def evaluate_for_stage(self, stage: str) -> Callable[..., Any]: ...
    def voters_for_round(self, round_id: str) -> list[dict[str, str]]: ...
    def judge_known_answer(self, sealed_case: Mapping[str, Any]) -> Mapping[str, str]: ...
    def final_audit(self, round_id: str) -> FinalAudit: ...


def _deadline_and_gates(round_record: Mapping[str, Any]) -> tuple[datetime, datetime, datetime]:
    schedule = schedule_for_round(round_record)
    return schedule.deadline, schedule.gold_close, schedule.publish_at


def _score_active_stage(
    round_id: str, *, backend: CompetitionBackend,
    inputs: TrustedRoundInputs, now: datetime, gold_close: datetime,
) -> dict[str, Any]:
    ledger = backend.list_stages(round_id)
    if not ledger:
        raise ValueError("active evaluation has no stage receipt")
    stage = ledger[-1]["stage"]
    if stage not in {"prescreen", "screen", "final", "week_one", "rerun"}:
        raise ValueError("active evaluation has an unknown stage")
    scoring_stage = ledger[-1].get("source_stage") if stage == "rerun" else stage
    identity_preparer = getattr(inputs, "prepare_identities", None)
    first = advance_ready_stage(round_id, backend=backend, identity_preparer=identity_preparer)
    if first["status"] == "started":
        return first
    challengers = {row["hotkey"] for row in ledger[-1]["entries"] if row["role"] == "challenger"}
    entries = {row["hotkey"] for row in ledger[-1]["entries"]}
    if not challengers or len(entries) != len(ledger[-1]["entries"]):
        raise ValueError("active stage has invalid frozen entrants")
    papers = [row for row in backend.list_sample(round_id) if row["stage"] == stage]
    expected = {(row["paper_id"], hotkey) for row in papers for hotkey in challengers}
    scored = {(row["paper_id"], row["challenger_hotkey"])
              for row in backend.list_stage_pairs(round_id, stage)}
    if not papers or len({row["paper_id"] for row in papers}) != len(papers) or not scored <= expected:
        raise ValueError("stage score grid differs from released sample")
    attempted = 0
    for paper in papers:
        paper_id = paper["paper_id"]
        if all((paper_id, hotkey) in scored for hotkey in challengers):
            continue
        jobs = backend.list_paper_jobs(round_id, stage, paper_id)
        if len(jobs) != len(entries) or {job["hotkey"] for job in jobs} != entries:
            continue
        if any(job["status"] != "completed" for job in jobs):
            continue
        score_ready_paper(
            round_id, stage, paper_id, backend=backend,
            bronze_artifact=inputs.bronze_for_paper(stage, paper_id),
            evaluate=inputs.evaluate_for_stage(scoring_stage),
        )
        attempted += 1
    if scoring_stage in {"prescreen", "screen"}:
        result = advance_ready_stage(round_id, backend=backend, identity_preparer=identity_preparer)
        if result["status"] == "started":
            return result
        return {**result, "scored_papers_this_tick": attempted}
    final_grid = {(row["paper_id"], row["challenger_hotkey"])
                  for row in backend.list_stage_pairs(round_id, stage)}
    if final_grid != expected:
        return {"status": "waiting_for_final_scores", "stage": stage,
                "scored_pairs": len(final_grid), "expected_pairs": len(expected),
                "scored_papers_this_tick": attempted}
    baseline = [row for row in ledger[-1]["entries"] if row["role"] in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("final stage has no unique baseline")
    if baseline[0]["role"] == "incumbent":
        final_pairs = backend.list_stage_pairs(round_id, stage)
        excluded = {row["paper_id"] for row in final_pairs if row["infrastructure_fault"]}
        failure = incumbent_failure_summary(
            round_id, stage=stage, paper_ids=[row["paper_id"] for row in papers],
            excluded_infrastructure_papers=excluded,
            incumbent_hotkey=baseline[0]["hotkey"], backend=backend,
        )
        if failure.reference_required:
            raise ReferenceFallbackRequired(failure)
    if now >= gold_close:
        return {"status": "missed_gold_window", "stage": stage}
    round_record = backend.get_round(round_id)
    profile = smoke_profile(round_record)
    evaluation = round_evaluation(round_record)
    known_answer_count = (evaluation.gold_known_answers if evaluation is not None
                          else profile.known_answer_count if profile is not None else 40)
    controller_review = round_record.get("rules_public", {}).get("policy", {}).get("review_execution") == "controller"
    if controller_review:
        source_status = inputs.prepare_consensus_sources(round_id)
        if source_status["status"] != "sources_ready":
            return source_status
    known_answers = build_known_answer_challenges(
        round_id,
        certified=backend.list_gold_known_answer_candidates(round_id),
        final_sample=papers, final_entries=ledger[-1]["entries"],
        seed=bytes.fromhex(round_record["drand_seed"]),
        limit=known_answer_count,
    )
    result = prepare_frozen_gold_review(
        round_id, backend=backend, voters=[] if controller_review else inputs.voters_for_round(round_id),
        known_answers=known_answers, minimum_known_answers=known_answer_count,
    )
    if result.get("status") == "waiting_for_known_answers":
        return result
    return {"status": "gold_open", "stage": stage,
            "case_count": result["case_count"]}


def tick_round(
    round_id: str, *, backend: CompetitionBackend,
    inputs: TrustedRoundInputs, now: datetime,
) -> dict[str, Any]:
    """Advance at most one round transition, or report the current wait gate."""
    if now.tzinfo is None:
        raise ValueError("controller clock must have a timezone")
    now = now.astimezone(UTC)
    round_record = backend.get_round(round_id)
    deadline, gold_close, publish_at = _deadline_and_gates(round_record)
    status = round_record["status"]
    if status == "open":
        if now < deadline:
            return {"status": "waiting_for_deadline", "deadline_at": deadline.isoformat()}
        return lock_ready_round(
            round_id, backend=backend, archive=inputs.archive, netuid=inputs.netuid,
            package_for_upload=inputs.package_for_submission,
        )
    if status == "locked":
        if inputs.chain.get_current_block() < int(round_record["reveal_block"]):
            return {"status": "waiting_for_reveal_block",
                    "reveal_block": round_record["reveal_block"]}
        if round_record.get("rules_public", {}).get("policy", {}).get("team_identity") == "linked":
            inputs.prepare_identities(round_id)
        return reveal_selected_sample(
            round_id, backend=backend, chain=inputs.chain,
            papers=inputs.papers_for_round(round_id),
            baseline=inputs.baseline_for_round(round_id),
            package_for_submission=inputs.package_for_submission,
            incumbent_package=inputs.incumbent_package_for_round(round_id),
            linked_pairs=inputs.linked_pairs_for_round(round_id),
            weekly_budget_usd=inputs.weekly_budget_for_round(round_id),
        )
    if status == "revealed":
        if round_record.get("rules_public", {}).get("policy", {}).get("team_identity") == "linked":
            has_prescreen = any(row["stage"] == "prescreen" for row in backend.list_sample(round_id))
            if not has_prescreen:
                identity = inputs.prepare_identities(round_id)
                if identity.get("status") == "waiting_for_team_review":
                    return identity
        if round_record.get("rules_public", {}).get("policy", {}).get("bronze", {}).get("mode") == "after_reveal":
            bronze = inputs.prepare_bronze(round_id)
            if bronze["status"] != "bronze_ready":
                return bronze
        return start_revealed_round(
            round_id, backend=backend, baseline=inputs.baseline_for_round(round_id),
            package_for_submission=inputs.package_for_submission,
            incumbent_package=inputs.incumbent_package_for_round(round_id),
            linked_pairs=inputs.linked_pairs_for_round(round_id),
        )
    if status in {"screening", "final"}:
        try:
            return _score_active_stage(
                round_id, backend=backend, inputs=inputs, now=now, gold_close=gold_close,
            )
        except ReferenceFallbackRequired as exc:
            return start_failed_incumbent_reference_rerun(
                round_id, backend=backend, failure=exc.summary,
                reference=inputs.reference_for_round(round_id),
            )
    if status == "gold":
        if now < gold_close:
            if round_record.get("rules_public", {}).get("policy", {}).get("review_execution") == "controller":
                return inputs.run_consensus(round_id)
            return {"status": "waiting_for_gold_close", "close_at": gold_close.isoformat()}
        return backend.close_gold_voting(round_id)
    if status == "gold_closed":
        if now < publish_at:
            return {"status": "waiting_for_publication", "publish_at": publish_at.isoformat()}
        gold = tally_closed_gold_review(round_id, backend=backend)
        candidates = frozen_gold_candidates(round_id, backend=backend)
        check_ids = sorted(
            row["case_id"] for row in backend.list_gold_cases(round_id, include_answers=True)
            if "check" in row["answer_private"].get("review_purposes", [])
        )
        audit = inputs.final_audit(round_id)
        copy_audit = audit_final_output_copies(round_id, backend=backend)
        security_audit = audit_final_worker_security(round_id, backend=backend)
        baseline_hotkeys = [row["hotkey"] for row in backend.list_stages(round_id)[-1]["entries"]
                            if row["role"] in {"incumbent", "reference"}]
        if len(baseline_hotkeys) != 1 or not security_audit[baseline_hotkeys[0]]["passed"]:
            raise ValueError("final baseline attempted denied egress; crown requires review")
        try:
            selection = select_ready_final(
                round_id, backend=backend,
                code_passed_by_hotkey={hotkey: passed and security_audit[hotkey]["passed"]
                                       for hotkey, passed in audit.code_passed_by_hotkey.items()},
                copy_by_hotkey={hotkey: row["is_copy"] for hotkey, row in copy_audit.items()},
                gold_by_hotkey=gold,
            )
        except ReferenceFallbackRequired as exc:
            return {"status": "reference_fallback_required",
                    "stage": exc.summary.stage,
                    "failed_papers": exc.summary.failed_papers,
                    "eligible_papers": exc.summary.eligible_papers}
        receipt = publish_selected_round(
            round_id, backend=backend, signer=inputs.signer, selection=selection,
            operator_hotkey=inputs.operator_hotkey,
            uid_by_hotkey=audit.uid_by_hotkey,
            qualified_by_hotkey=audit.qualified_by_hotkey,
            production_team_id=audit.production_team_id,
            gold_by_hotkey=gold,
            copy_audit=copy_audit,
            worker_security_audit=security_audit,
            code_audit={
                "evidence_sha256": audit.code_evidence_sha256,
                "failed_checks": dict(audit.code_failure_counts_by_hotkey),
            },
            gold_check={
                "seed": round_record["gold_check_seed_private"],
                "commitment": round_record["gold_check_commitment"],
                "candidate_ids": sorted(candidate.case.case_id for candidate in candidates),
                "case_ids": check_ids,
            },
            known_answer_audit=audit_closed_known_answers(
                round_id, backend=backend, tiebreak_judge=inputs.judge_known_answer,
            ),
        )
        return {"status": "published", "bundle_sha256": receipt["bundle_sha256"]}
    if status == "published":
        return {"status": "already_published"}
    raise ValueError("round has an unknown competition status")
