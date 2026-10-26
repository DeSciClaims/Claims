"""One resumable tick of the trusted weekly competition controller.

An external timer calls ``tick_round`` repeatedly. All durable transitions
live in the backend, so a controller restart rereads the round and resumes.
Miner code and inference credentials never enter this process's callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .drand import BlockClock
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
from .sample_release import PaperRecord
from .stage_controller import advance_ready_stage


@dataclass(frozen=True)
class FinalAudit:
    code_passed_by_hotkey: Mapping[str, bool]
    copy_by_hotkey: Mapping[str, bool]
    qualified_by_hotkey: Mapping[str, bool]
    uid_by_hotkey: Mapping[str, int]
    production_team_id: str | None


class TrustedRoundInputs(Protocol):
    archive: ArchiveReader
    chain: BlockClock
    netuid: int
    signer: Any
    operator_hotkey: str

    def package_for_submission(self, submission: Mapping[str, Any]) -> bytes: ...
    def papers_for_round(self, round_id: str) -> list[PaperRecord]: ...
    def baseline_for_round(self, round_id: str) -> Mapping[str, Any]: ...
    def incumbent_package_for_round(self, round_id: str) -> bytes | None: ...
    def linked_pairs_for_round(self, round_id: str) -> list[tuple[str, str, str]]: ...
    def weekly_budget_for_round(self, round_id: str) -> float: ...
    def bronze_for_paper(self, stage: str, paper_id: str) -> Mapping[str, Any]: ...
    def evaluate_for_stage(self, stage: str) -> Callable[..., Any]: ...
    def voters_for_round(self, round_id: str) -> list[dict[str, str]]: ...
    def judge_known_answer(self, sealed_case: Mapping[str, Any]) -> Mapping[str, str]: ...
    def final_audit(self, round_id: str) -> FinalAudit: ...


def _deadline_and_gates(round_record: Mapping[str, Any]) -> tuple[datetime, datetime, datetime]:
    deadline = datetime.fromisoformat(str(round_record["deadline_at"]))
    if deadline.tzinfo is None:
        raise ValueError("competition deadline must have a timezone")
    deadline = deadline.astimezone(UTC)
    if (deadline.weekday(), deadline.hour, deadline.minute, deadline.second) != (0, 12, 0, 0):
        raise ValueError("competition deadline must be Monday 12:00 UTC")
    close = deadline + timedelta(days=3)
    return deadline, close, close + timedelta(hours=6)


def _score_active_stage(
    round_id: str, *, backend: CompetitionBackend,
    inputs: TrustedRoundInputs, now: datetime, gold_close: datetime,
) -> dict[str, Any]:
    ledger = backend.list_stages(round_id)
    if not ledger:
        raise ValueError("active evaluation has no stage receipt")
    stage = ledger[-1]["stage"]
    if stage not in {"prescreen", "screen", "final", "week_one"}:
        raise ValueError("active evaluation has an unknown stage")
    first = advance_ready_stage(round_id, backend=backend)
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
            evaluate=inputs.evaluate_for_stage(stage),
        )
        attempted += 1
    if stage in {"prescreen", "screen"}:
        result = advance_ready_stage(round_id, backend=backend)
        if result["status"] == "started":
            return result
        return {**result, "scored_papers_this_tick": attempted}
    final_grid = {(row["paper_id"], row["challenger_hotkey"])
                  for row in backend.list_stage_pairs(round_id, stage)}
    if final_grid != expected:
        return {"status": "waiting_for_final_scores", "stage": stage,
                "scored_pairs": len(final_grid), "expected_pairs": len(expected),
                "scored_papers_this_tick": attempted}
    if now >= gold_close:
        return {"status": "missed_gold_window", "stage": stage}
    round_record = backend.get_round(round_id)
    known_answers = build_known_answer_challenges(
        round_id,
        certified=backend.list_gold_known_answer_candidates(round_id),
        final_sample=papers, final_entries=ledger[-1]["entries"],
        seed=bytes.fromhex(round_record["drand_seed"]),
    )
    result = prepare_frozen_gold_review(
        round_id, backend=backend, voters=inputs.voters_for_round(round_id),
        known_answers=known_answers, minimum_known_answers=40,
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
        return start_revealed_round(
            round_id, backend=backend, baseline=inputs.baseline_for_round(round_id),
            package_for_submission=inputs.package_for_submission,
            incumbent_package=inputs.incumbent_package_for_round(round_id),
            linked_pairs=inputs.linked_pairs_for_round(round_id),
        )
    if status in {"screening", "final"}:
        return _score_active_stage(
            round_id, backend=backend, inputs=inputs, now=now, gold_close=gold_close,
        )
    if status == "gold":
        if now < gold_close:
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
        selection = select_ready_final(
            round_id, backend=backend,
            code_passed_by_hotkey=audit.code_passed_by_hotkey,
            copy_by_hotkey=audit.copy_by_hotkey, gold_by_hotkey=gold,
        )
        receipt = publish_selected_round(
            round_id, backend=backend, signer=inputs.signer, selection=selection,
            operator_hotkey=inputs.operator_hotkey,
            uid_by_hotkey=audit.uid_by_hotkey,
            qualified_by_hotkey=audit.qualified_by_hotkey,
            production_team_id=audit.production_team_id,
            gold_by_hotkey=gold,
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
