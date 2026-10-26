"""Read the frozen final grid and apply the weekly crown policy."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .final_selection import FinalSelection, Finalist, select_final_production
from .gold import GoldResult
from .policy import PaperPairScore


class FinalBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_stage_pairs(self, round_id: str, stage: str) -> list[dict[str, Any]]: ...
    def list_submissions(self, round_id: str) -> list[dict[str, Any]]: ...


def select_ready_final(
    round_id: str, *, backend: FinalBackend,
    code_passed_by_hotkey: Mapping[str, bool], copy_by_hotkey: Mapping[str, bool],
    gold_by_hotkey: Mapping[str, GoldResult],
) -> FinalSelection:
    """Require every final pair and audit verdict before choosing production."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold_closed"}:
        raise ValueError("final round has not finished evaluation or Gold voting")
    stage = "week_one" if round_record["incumbent_hotkey"] is None else "final"
    ledgers = backend.list_stages(round_id)
    if not ledgers or ledgers[-1]["stage"] != stage:
        raise ValueError("final stage has not started")
    entries = ledgers[-1]["entries"]
    baseline = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    challengers = [row for row in entries if row["role"] == "challenger"]
    if len(baseline) != 1 or not challengers:
        raise ValueError("final stage needs one baseline and at least one finalist")
    if (stage == "final" and baseline[0]["hotkey"] != round_record["incumbent_hotkey"]):
        raise ValueError("final baseline differs from frozen incumbent")
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
    faults = {paper_id: any(grid[(paper_id, row["hotkey"])]["infrastructure_fault"]
                            for row in challengers) for paper_id in paper_ids}
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
    return select_final_production(
        finalists, incumbent_hotkey=round_record["incumbent_hotkey"],
        incumbent_team_id=baseline[0]["team_id"] if stage == "final" else None,
    )
