"""Select a production winner from a second validator's own frozen score grid."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .failure_policy import ReferenceFallbackRequired, incumbent_failure_summary
from .final_selection import FinalSelection, Finalist, select_final_production
from .final_stage import effective_final_stage
from .gold import GoldResult
from .policy import PaperPairScore


def select_replay_final(
    round_id: str, *, backend: Any,
    gold_by_hotkey: Mapping[str, GoldResult],
) -> FinalSelection:
    """Apply the normal crown rules to local pairs and a supplied Gold outcome.

    The caller must establish and freeze the Gold review policy separately.
    This function cannot treat another run's unmatched cases as local votes.
    """
    record = backend.get_round(round_id)
    ledger = backend.list_stages(round_id)
    stage = effective_final_stage(ledger)
    entries = ledger[-1]["entries"]
    baselines = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    challengers = [row for row in entries if row["role"] == "challenger"]
    if len(baselines) != 1 or not challengers:
        raise ValueError("replay final has invalid entrants")
    baseline = baselines[0]
    if (baseline["package_sha256"] != record[
        "baseline_package_sha256" if baseline["role"] == "incumbent"
        else "reference_package_sha256"
    ] or (baseline["role"] == "incumbent"
          and baseline["hotkey"] != record["incumbent_hotkey"])):
        raise ValueError("replay final baseline differs from frozen round")
    audit = backend.get_final_audit(round_id)
    if audit is None or not isinstance(audit.get("result_private"), dict):
        raise ValueError("replay final audit is missing")
    result = audit["result_private"]
    finalist_hotkeys = {row["hotkey"] for row in challengers}
    code = result.get("code_passed")
    copy = result.get("copy_audit")
    security = result.get("worker_security_audit")
    if (result.get("round_id") != round_id or result.get("stage") != stage
        or result.get("configuration_sha256") != record["configuration_sha256"]
        or not isinstance(code, dict) or set(code) != finalist_hotkeys
        or not isinstance(copy, dict) or set(copy) != finalist_hotkeys
        or not isinstance(security, dict)
        or not (finalist_hotkeys | {baseline["hotkey"]}) <= set(security)
        or security[baseline["hotkey"]].get("passed") is not True
        or any(type(code[hotkey]) is not bool
               or type(copy[hotkey].get("is_copy")) is not bool
               or type(security[hotkey].get("passed")) is not bool
               for hotkey in finalist_hotkeys)):
        raise ValueError("replay final audit is incomplete or unsafe")
    if set(gold_by_hotkey) != finalist_hotkeys or any(
        not isinstance(gold_by_hotkey[hotkey], GoldResult)
        for hotkey in finalist_hotkeys
    ):
        raise ValueError("replay Gold outcome must cover each local finalist")
    papers = [row["paper_id"] for row in backend.list_sample(round_id)
              if row["stage"] == stage]
    if not papers or len(set(papers)) != len(papers):
        raise ValueError("replay final sample is invalid")
    expected = {(paper_id, hotkey) for paper_id in papers
                for hotkey in finalist_hotkeys}
    grid = {}
    by_hotkey = {row["hotkey"]: row for row in challengers}
    for pair in backend.list_stage_pairs(round_id, stage):
        key = pair["paper_id"], pair["challenger_hotkey"]
        if (key not in expected or key in grid or pair["stage"] != stage
            or pair["baseline_hotkey"] != baseline["hotkey"]
            or pair["baseline_package_sha256"] != baseline["package_sha256"]
            or pair["challenger_package_sha256"]
                != by_hotkey[pair["challenger_hotkey"]]["package_sha256"]):
            raise ValueError("replay final pair differs from frozen entrants")
        grid[key] = pair
    if set(grid) != expected:
        raise ValueError("replay final pair grid is incomplete")
    faults = {paper_id: any(grid[(paper_id, hotkey)]["infrastructure_fault"]
                            for hotkey in finalist_hotkeys) for paper_id in papers}
    if baseline["role"] == "incumbent":
        failure = incumbent_failure_summary(
            round_id, stage=stage, paper_ids=papers,
            excluded_infrastructure_papers={paper_id for paper_id in papers
                                            if faults[paper_id]},
            incumbent_hotkey=baseline["hotkey"], backend=backend,
        )
        if failure.reference_required:
            raise ReferenceFallbackRequired(failure)
    committed = {row["hotkey"]: row["commit_block"]
                 for row in backend.list_submissions(round_id)
                 if row["status"] == "accepted" and row["commit_block"] is not None}
    finalists = []
    for entry in challengers:
        hotkey = entry["hotkey"]
        if hotkey not in committed:
            raise ValueError("replay finalist has no frozen commitment")
        finalists.append(Finalist(
            hotkey, entry["team_id"], int(committed[hotkey]),
            tuple(PaperPairScore(
                paper_id, grid[(paper_id, hotkey)]["challenger_score"],
                grid[(paper_id, hotkey)]["baseline_score"], faults[paper_id],
            ) for paper_id in papers),
            code[hotkey] and security[hotkey]["passed"],
            copy[hotkey]["is_copy"], gold_by_hotkey[hotkey].refusal,
        ))
    selection = select_final_production(
        finalists,
        incumbent_hotkey=record["incumbent_hotkey"]
        if baseline["role"] == "incumbent" else None,
        incumbent_team_id=baseline["team_id"]
        if baseline["role"] == "incumbent" else None,
    )
    if (baseline["role"] == "reference" and record["incumbent_hotkey"] is not None
        and selection.decision == "no_crown"):
        selection = replace(selection, decision="reference_fallback")
    return replace(selection, final_pair_rows=tuple(grid[key] for key in sorted(grid)))
