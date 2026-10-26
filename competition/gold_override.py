"""Deterministically apply closed escalation votes to frozen final Silver scores."""

from __future__ import annotations

from typing import Any, Mapping

from validator.agent_v1.comparison_models import ComparisonCandidate, SilverRecord
from validator.agent_v1.models import AgentV1ValidationFinding

from .gold import VoteDecision
from .scoring import score_pairwise_paper


def rescore_final_paper_from_escalations(
    frozen_result: dict[str, Any], *,
    sealed_cases: list[dict[str, Any]], decisions: Mapping[str, VoteDecision],
) -> list[dict[str, Any]]:
    """Recompute affected pairs from frozen Silver, with no new judge calls.

    A contested accepted unit is removed when a decided eligible vote rejects
    its original pairwise owner. Gold-check-only cases do not affect scores.
    """
    silver_private = frozen_result.get("silver")
    if silver_private is None:
        return list(frozen_result["pairs"])
    silver = SilverRecord.model_validate(silver_private["silver_record"])
    submissions = {
        row["miner_id"]: row for row in silver_private["miner_submissions"]
    }
    if len(submissions) != len(silver_private["miner_submissions"]):
        raise ValueError("frozen final has duplicate miner submissions")
    units = {unit.silver_unit_id for unit in silver.silver_units}
    cases_by_pair: dict[str, list[dict[str, Any]]] = {}
    for case in sealed_cases:
        if case["paper_id"] != silver.paper_id:
            raise ValueError("Gold override case belongs to another paper")
        if "escalation" not in case["answer_private"].get("review_purposes", []):
            continue
        answer = case["answer_private"]
        if answer["silver_unit_id"] not in units or answer["tiebreak"] not in {"challenger", "incumbent"}:
            raise ValueError("Gold override has invalid frozen Silver answer")
        if case["case_id"] not in decisions:
            raise ValueError("Gold override has no closed vote decision")
        cases_by_pair.setdefault(case["challenger_hotkey"], []).append(case)
    rescored = []
    for original in frozen_result["pairs"]:
        hotkey = original["challenger_hotkey"]
        pair_cases = cases_by_pair.pop(hotkey, [])
        if not pair_cases or original["infrastructure_fault"]:
            rescored.append(original)
            continue
        removed: set[str] = set()
        seen: set[str] = set()
        for case in pair_cases:
            if case["incumbent_hotkey"] != original["baseline_hotkey"]:
                raise ValueError("Gold override case has a different incumbent")
            unit_id = case["answer_private"]["silver_unit_id"]
            if unit_id in seen:
                raise ValueError("Gold override has duplicate pairwise Silver units")
            seen.add(unit_id)
            choice = decisions[case["case_id"]].choice
            if choice is not None and choice != case["answer_private"]["tiebreak"]:
                removed.add(unit_id)
        if not removed:
            rescored.append(original)
            continue
        revised_silver = silver.model_copy(deep=True)
        revised_silver.silver_units = [unit for unit in revised_silver.silver_units
                                       if unit.silver_unit_id not in removed]
        challenger = submissions.get(hotkey)
        incumbent = submissions.get(original["baseline_hotkey"])
        pair = score_pairwise_paper(
            challenger_id=hotkey, incumbent_id=original["baseline_hotkey"],
            silver_record=revised_silver,
            challenger_candidates=[ComparisonCandidate.model_validate(row)
                                   for row in challenger["candidates"]] if challenger else [],
            incumbent_candidates=[ComparisonCandidate.model_validate(row)
                                  for row in incumbent["candidates"]] if incumbent else [],
            challenger_findings=[AgentV1ValidationFinding.model_validate(row)
                                 for row in challenger["normal_findings"]] if challenger else [],
            incumbent_findings=[AgentV1ValidationFinding.model_validate(row)
                                for row in incumbent["normal_findings"]] if incumbent else [],
        )
        rescored.append({
            **original,
            "challenger_score": pair.challenger.score if challenger else 0.0,
            "baseline_score": pair.incumbent.score if incumbent else 0.0,
            "details_public": {
                **original["details_public"],
                "gold_override_count": len(removed),
            },
        })
    if cases_by_pair:
        raise ValueError("Gold override case has no frozen final pair")
    return rescored
