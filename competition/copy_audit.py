"""Recompute finalist output-copy verdicts from frozen final Silver receipts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Protocol

from .copy_check import output_copy_verdict
from .policy import PaperPairScore


class CopyAuditBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_stage_pairs(self, round_id: str, stage: str) -> list[dict[str, Any]]: ...
    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None: ...


def _accepted_units(silver: dict[str, Any], paper_id: str,
                    expected_hotkeys: set[str]) -> dict[str, set[str]]:
    if not isinstance(silver, dict):
        raise ValueError("frozen paper has no final Silver record")
    record = silver.get("silver_record")
    submissions = silver.get("miner_submissions")
    if (not isinstance(record, dict) or record.get("paper_id") != paper_id
        or not isinstance(submissions, list)):
        raise ValueError("frozen paper has no final Silver record or miner submissions")
    candidates: dict[str, set[str]] = {hotkey: set() for hotkey in expected_hotkeys}
    seen: set[str] = set()
    seen_candidate_ids: set[str] = set()
    for row in submissions:
        if not isinstance(row, dict) or row.get("miner_id") not in candidates:
            raise ValueError("frozen Silver submission differs from final entrants")
        hotkey = row["miner_id"]
        if hotkey in seen or not isinstance(row.get("candidates"), list):
            raise ValueError("frozen Silver submissions are duplicated or malformed")
        seen.add(hotkey)
        for candidate in row["candidates"]:
            candidate_id = candidate.get("candidate_id") if isinstance(candidate, dict) else None
            if (not isinstance(candidate_id, str) or not candidate_id
                or candidate_id in seen_candidate_ids
                or candidate.get("paper_id") not in {None, paper_id}):
                raise ValueError("frozen Silver candidate ID is duplicated or malformed")
            candidates[hotkey].add(candidate_id)
            seen_candidate_ids.add(candidate_id)
    accepted: dict[str, set[str]] = {hotkey: set() for hotkey in expected_hotkeys}
    units = record.get("silver_units")
    if not isinstance(units, list):
        raise ValueError("frozen Silver units are malformed")
    seen_units: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("frozen Silver unit is malformed")
        unit_id = unit.get("silver_unit_id")
        equivalents = unit.get("equivalent_candidate_ids")
        if (not isinstance(unit_id, str) or not unit_id or unit_id in seen_units
            or unit.get("paper_id") != paper_id
            or not isinstance(equivalents, list)
            or any(not isinstance(value, str) for value in equivalents)):
            raise ValueError("frozen Silver unit identity is malformed")
        seen_units.add(unit_id)
        if not unit.get("required_for_completeness") and unit.get("scoring_mode") != "accepted_improvement":
            continue
        equivalent_ids = set(equivalents)
        for hotkey, candidate_ids in candidates.items():
            if candidate_ids & equivalent_ids:
                accepted[hotkey].add(unit_id)
    return accepted


def audit_final_output_copies(round_id: str, *, backend: CopyAuditBackend) -> dict[str, dict[str, Any]]:
    """Return public-safe evidence for every finalist against the frozen baseline."""
    round_record = backend.get_round(round_id)
    stage = "week_one" if round_record.get("incumbent_hotkey") is None else "final"
    ledgers = [row for row in backend.list_stages(round_id) if row.get("stage") == stage]
    if len(ledgers) != 1:
        raise ValueError("copy audit has no unique frozen final stage")
    entries = ledgers[0]["entries"]
    baselines = [row["hotkey"] for row in entries if row["role"] in {"reference", "incumbent"}]
    challengers = [row["hotkey"] for row in entries if row["role"] == "challenger"]
    if (len(baselines) != 1 or not challengers or len(set(challengers)) != len(challengers)
        or baselines[0] in challengers):
        raise ValueError("copy audit final entrants are malformed")
    baseline = baselines[0]
    expected_hotkeys = {baseline, *challengers}
    sample = [row for row in backend.list_sample(round_id) if row["stage"] == stage]
    paper_ids = [row["paper_id"] for row in sample]
    if not paper_ids or len(set(paper_ids)) != len(paper_ids):
        raise ValueError("copy audit final sample is empty or duplicated")
    pairs = {(row["paper_id"], row["challenger_hotkey"]): row
             for row in backend.list_stage_pairs(round_id, stage)}
    if len(pairs) != len(paper_ids) * len(challengers) or set(pairs) != {
        (paper_id, hotkey) for paper_id in paper_ids for hotkey in challengers
    }:
        raise ValueError("copy audit final pair grid is incomplete or duplicated")
    accepted: dict[str, dict[str, set[str]]] = {hotkey: {} for hotkey in expected_hotkeys}
    for paper_id in paper_ids:
        fault = any(bool(pairs[(paper_id, hotkey)]["infrastructure_fault"])
                    for hotkey in challengers)
        receipt = backend.get_paper_evaluation(round_id, stage, paper_id)
        if receipt is None or not isinstance(receipt.get("result_private"), dict):
            raise ValueError("copy audit paper has no frozen evaluation")
        result = receipt["result_private"]
        if fault:
            if result.get("silver") is not None:
                raise ValueError("infrastructure-fault paper has a Silver record")
            units = {hotkey: set() for hotkey in expected_hotkeys}
        else:
            units = _accepted_units(result.get("silver"), paper_id, expected_hotkeys)
        for hotkey in expected_hotkeys:
            accepted[hotkey][paper_id] = units[hotkey]
    verdicts = {}
    for hotkey in challengers:
        scored = [PaperPairScore(
            paper_id, pairs[(paper_id, hotkey)]["challenger_score"],
            pairs[(paper_id, hotkey)]["baseline_score"],
            bool(pairs[(paper_id, hotkey)]["infrastructure_fault"]),
        ) for paper_id in paper_ids]
        verdicts[hotkey] = asdict(output_copy_verdict(
            candidate_classes=accepted[hotkey], comparator_classes=accepted[baseline],
            paired_scores=scored,
        ))
    return verdicts
