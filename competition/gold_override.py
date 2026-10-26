"""Deterministically apply closed escalation votes to frozen final Silver scores."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from validator.agent_v1.comparison_models import ComparisonCandidate, SilverRecord, SilverUnit
from validator.agent_v1.models import AgentV1ValidationFinding

from .gold import VoteDecision
from .scoring import score_pairwise_paper


def rescore_final_paper_from_escalations(
    frozen_result: dict[str, Any], *,
    sealed_cases: list[dict[str, Any]], decisions: Mapping[str, VoteDecision],
) -> list[dict[str, Any]]:
    """Recompute affected pairs from frozen Silver, with no new judge calls.

    A decided vote can remove an accepted unit or accept a previously rejected
    miner claim, then recompute the same frozen pair. Check-only cases do not
    affect scores.
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
    invalid_ids = {(row.candidate_id, row.miner_id)
                   for row in silver.invalid_miner_candidates}
    cases_by_pair: dict[str, list[dict[str, Any]]] = {}
    for case in sealed_cases:
        if case["paper_id"] != silver.paper_id:
            raise ValueError("Gold override case belongs to another paper")
        if "escalation" not in case["answer_private"].get("review_purposes", []):
            continue
        answer = case["answer_private"]
        kind = answer.get("kind", "silver_unit")
        if (answer["tiebreak"] not in {"challenger", "incumbent"}
            or kind not in {"silver_unit", "invalid_candidate"}):
            raise ValueError("Gold override has invalid frozen Silver answer")
        if kind == "silver_unit" and answer.get("silver_unit_id") not in units:
            raise ValueError("Gold override has no frozen Silver unit")
        if kind == "invalid_candidate" and not any(
            candidate_id == answer.get("invalid_candidate_id")
            and miner_id in {case["challenger_hotkey"], case["incumbent_hotkey"]}
            for candidate_id, miner_id in invalid_ids
        ):
            raise ValueError("Gold override has no frozen invalid candidate")
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
        accepted_invalid: set[str] = set()
        seen: set[str] = set()
        for case in pair_cases:
            if case["incumbent_hotkey"] != original["baseline_hotkey"]:
                raise ValueError("Gold override case has a different incumbent")
            answer = case["answer_private"]
            kind = answer.get("kind", "silver_unit")
            target = answer["silver_unit_id"] if kind == "silver_unit" else answer["invalid_candidate_id"]
            if target in seen:
                raise ValueError("Gold override has duplicate pairwise targets")
            seen.add(target)
            choice = decisions[case["case_id"]].choice
            if kind == "silver_unit" and choice is not None and choice != answer["tiebreak"]:
                removed.add(target)
            if kind == "invalid_candidate" and choice in {"challenger", "incumbent"} and choice != answer["tiebreak"]:
                accepted_invalid.add(target)
        if not removed and not accepted_invalid:
            rescored.append(original)
            continue
        revised_silver = silver.model_copy(deep=True)
        revised_silver.silver_units = [unit for unit in revised_silver.silver_units
                                       if unit.silver_unit_id not in removed]
        if accepted_invalid:
            candidates = {
                candidate.candidate_id: candidate
                for submission in submissions.values()
                for row in submission["candidates"]
                for candidate in (ComparisonCandidate.model_validate(row),)
            }
            for candidate_id in sorted(accepted_invalid):
                invalid = next((row for row in revised_silver.invalid_miner_candidates
                                if row.candidate_id == candidate_id
                                and row.miner_id in {hotkey, original["baseline_hotkey"]}), None)
                candidate = candidates.get(candidate_id)
                if invalid is None or candidate is None or candidate.miner_id != invalid.miner_id:
                    raise ValueError("Gold accepted candidate differs from frozen miner submission")
                if any(candidate_id in unit.equivalent_candidate_ids for unit in revised_silver.silver_units):
                    raise ValueError("Gold accepted candidate already has a Silver unit")
                revised_silver.invalid_miner_candidates.remove(invalid)
                group_rows = revised_silver.metadata.get("comparison_equivalence_groups", {}).get("groups", [])
                group_ids = [row["group_id"] for row in group_rows
                             if candidate_id in row.get("member_candidate_ids", [])]
                digest = hashlib.sha256(
                    (revised_silver.paper_id + "\0" + candidate_id).encode(),
                ).hexdigest()[:16]
                revised_silver.silver_units.append(SilverUnit(
                    silver_unit_id=f"gold_accept_{digest}", paper_id=revised_silver.paper_id,
                    statement=candidate.statement, importance=candidate.importance,
                    required_for_completeness=False, scoring_mode="accepted_improvement",
                    equivalent_candidate_ids=[candidate_id], evidence_ids=candidate.evidence_ids,
                    source_span_ids=candidate.source_span_ids, source_quotes=candidate.source_quotes,
                    adjudication_case_ids=[invalid.adjudication_case_id]
                    if invalid.adjudication_case_id else [],
                    metadata={
                        "candidate_provenance": [{
                            "candidate_id": candidate.candidate_id, "origin": candidate.origin,
                            "miner_id": candidate.miner_id, "record_id": candidate.record_id,
                            "statement": candidate.statement, "evidence_ids": candidate.evidence_ids,
                            "source_span_ids": candidate.source_span_ids,
                        }],
                        "comparison_equivalence_group_ids": group_ids,
                    },
                ))
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
                "gold_override_count": len(removed) + len(accepted_invalid),
            },
        })
    if cases_by_pair:
        raise ValueError("Gold override case has no frozen final pair")
    return rescored
