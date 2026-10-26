"""Read the frozen final grid and apply the weekly crown policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Protocol

from .final_selection import FinalSelection, Finalist, select_final_production
from .failure_policy import ReferenceFallbackRequired, incumbent_failure_summary
from .final_stage import effective_final_stage
from .gold import GoldResult
from .gold_controller import closed_gold_decisions
from .gold_override import rescore_final_paper_from_escalations
from .policy import PaperPairScore


class FinalBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_stage_pairs(self, round_id: str, stage: str) -> list[dict[str, Any]]: ...
    def list_submissions(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_cases(self, round_id: str, *, include_answers: bool = False) -> list[dict[str, Any]]: ...
    def list_gold_voters(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_votes(self, round_id: str) -> list[dict[str, Any]]: ...
    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None: ...
    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]: ...


def select_ready_final(
    round_id: str, *, backend: FinalBackend,
    code_passed_by_hotkey: Mapping[str, bool], copy_by_hotkey: Mapping[str, bool],
    gold_by_hotkey: Mapping[str, GoldResult],
) -> FinalSelection:
    """Require every final pair and audit verdict before choosing production."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold_closed"}:
        raise ValueError("final round has not finished evaluation or Gold voting")
    ledgers = backend.list_stages(round_id)
    stage = effective_final_stage(ledgers)
    entries = ledgers[-1]["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    challengers = [row for row in entries if row["role"] == "challenger"]
    if len(baseline) != 1 or not challengers:
        raise ValueError("final stage needs one baseline and at least one finalist")
    if baseline[0]["role"] == "incumbent":
        if (baseline[0]["hotkey"] != round_record["incumbent_hotkey"]
            or baseline[0]["package_sha256"] != round_record["baseline_package_sha256"]):
            raise ValueError("final baseline differs from frozen incumbent")
    elif (baseline[0]["package_sha256"] != round_record["reference_package_sha256"]
          or (round_record["incumbent_hotkey"] is not None
              and not any(row["stage"] == "rerun" for row in ledgers))):
        raise ValueError("final reference differs from frozen fallback")
    if set(code_passed_by_hotkey) != {row["hotkey"] for row in challengers} or set(copy_by_hotkey) != set(code_passed_by_hotkey):
        raise ValueError("finalist code and copy verdicts must be complete")
    if round_record["status"] == "gold_closed":
        if set(gold_by_hotkey) != {row["hotkey"] for row in challengers}:
            raise ValueError("closed Gold results must cover every finalist")
    elif gold_by_hotkey:
        raise ValueError("open Gold review cannot affect the final")
    paper_ids = [row["paper_id"] for row in backend.list_sample(round_id) if row["stage"] == stage]
    if not paper_ids or len(paper_ids) != len(set(paper_ids)):
        raise ValueError("final sample needs distinct papers")
    expected = {(paper_id, row["hotkey"]) for paper_id in paper_ids for row in challengers}
    grid: dict[tuple[str, str], dict[str, Any]] = {}
    for row in backend.list_stage_pairs(round_id, stage):
        key = (row["paper_id"], row["challenger_hotkey"])
        if (key not in expected or key in grid or row["stage"] != stage
            or row["baseline_hotkey"] != baseline[0]["hotkey"]
            or row["baseline_package_sha256"] != baseline[0]["package_sha256"]):
            raise ValueError("final pair grid differs from frozen entrants")
        challenger = next(item for item in challengers if item["hotkey"] == row["challenger_hotkey"])
        if row["challenger_package_sha256"] != challenger["package_sha256"]:
            raise ValueError("final pair package differs from frozen finalist")
        grid[key] = row
    if set(grid) != expected:
        raise ValueError("final pair grid is incomplete")
    if round_record["status"] == "gold_closed":
        cases = backend.list_gold_cases(round_id, include_answers=True)
        escalation_cases = [case for case in cases
                            if "escalation" in case["answer_private"].get("review_purposes", [])]
        if escalation_cases:
            decisions = closed_gold_decisions(round_id, backend=backend)
            by_paper: dict[str, list[dict[str, Any]]] = {}
            for case in cases:
                by_paper.setdefault(case["paper_id"], []).append(case)
            for paper_id in {case["paper_id"] for case in escalation_cases}:
                receipt = backend.get_paper_evaluation(round_id, stage, paper_id)
                if receipt is None:
                    raise ValueError("escalated final paper has no frozen evaluation")
                frozen_result = receipt["result_private"]
                for original in frozen_result["pairs"]:
                    key = (paper_id, original["challenger_hotkey"])
                    stored = grid.get(key)
                    if (stored is None or original["baseline_hotkey"] != stored["baseline_hotkey"]
                        or original["challenger_score"] != stored["challenger_score"]
                        or original["baseline_score"] != stored["baseline_score"]):
                        raise ValueError("provisional final pair differs from frozen evaluation")
                for adjusted in rescore_final_paper_from_escalations(
                    frozen_result, sealed_cases=by_paper[paper_id], decisions=decisions,
                ):
                    key = (paper_id, adjusted["challenger_hotkey"])
                    grid[key] = {**grid[key], "challenger_score": adjusted["challenger_score"],
                                 "baseline_score": adjusted["baseline_score"]}
    faults = {paper_id: any(grid[(paper_id, row["hotkey"])]["infrastructure_fault"]
                            for row in challengers) for paper_id in paper_ids}
    if baseline[0]["role"] == "incumbent":
        failure = incumbent_failure_summary(
            round_id, stage=stage, paper_ids=paper_ids,
            excluded_infrastructure_papers={paper_id for paper_id, fault in faults.items() if fault},
            incumbent_hotkey=baseline[0]["hotkey"], backend=backend,
        )
        if failure.reference_required:
            raise ReferenceFallbackRequired(failure)
    committed = {row["hotkey"]: row["commit_block"] for row in backend.list_submissions(round_id)
                 if row["status"] == "accepted" and row["commit_block"] is not None}
    finalists: list[Finalist] = []
    for row in challengers:
        hotkey = row["hotkey"]
        if hotkey not in committed:
            raise ValueError("finalist commitment is missing")
        papers = tuple(PaperPairScore(
            paper_id, grid[(paper_id, hotkey)]["challenger_score"],
            grid[(paper_id, hotkey)]["baseline_score"], faults[paper_id],
        ) for paper_id in paper_ids)
        finalists.append(Finalist(
            hotkey, row["team_id"], int(committed[hotkey]), papers,
            code_passed_by_hotkey[hotkey], copy_by_hotkey[hotkey],
            gold_by_hotkey[hotkey].refusal if hotkey in gold_by_hotkey else False,
        ))
    selection = select_final_production(
        finalists,
        incumbent_hotkey=round_record["incumbent_hotkey"] if baseline[0]["role"] == "incumbent" else None,
        incumbent_team_id=baseline[0]["team_id"] if baseline[0]["role"] == "incumbent" else None,
    )
    if (baseline[0]["role"] == "reference" and round_record["incumbent_hotkey"] is not None
        and selection.decision == "no_crown"):
        selection = replace(selection, decision="reference_fallback")
    return replace(selection, final_pair_rows=tuple(grid[key] for key in sorted(grid)))
