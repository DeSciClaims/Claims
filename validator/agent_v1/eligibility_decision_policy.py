"""Optional early-stage routing through the shared Decisions eligibility adapter."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

from .adjudication_models import AdjudicationContextBundle
from .eligibility import (
    EligibilityAdjudicationDecision,
    decide_eligibility_adjudication,
    eligibility_adjudication_vote_from_assessment,
)
from .eligibility_decisions import DecisionsEligibilityRuntime


@dataclass(frozen=True)
class EligibilityDecisionPolicy:
    runtime: DecisionsEligibilityRuntime = field(repr=False)
    api_key: str = field(repr=False)
    mode: Literal["top_fraction", "confidence_threshold"]
    cutoff: float
    max_workers: int = 1

    def __post_init__(self) -> None:
        if (
            not self.api_key
            or self.mode not in {"top_fraction", "confidence_threshold"}
            or type(self.cutoff) not in {int, float}
            or not math.isfinite(self.cutoff)
            or not 0 <= self.cutoff <= 1
            or type(self.max_workers) is not int
            or not 1 <= self.max_workers <= 32
        ):
            raise ValueError("Decision eligibility routing policy is invalid")

    def route(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        paper_id: str,
        workspace_id: str,
        prepare_task: Callable,
        validate_output: Callable,
        skill_instructions: Callable[[str], str],
    ) -> tuple[dict[str, EligibilityAdjudicationDecision], dict[str, Any]]:
        """Keep the normal two-role eligibility contract; send splits/errors to full judges.

        The fraction is per paper, with floor(fraction * all cases) as the
        maximum shortcut count. Failed, missing-source or disagreeing cases
        never consume a shortcut slot or get accepted without full judging.
        """

        def probe(context, role):
            task, aliases, ids = prepare_task([context])
            if not task["source_spans"]:
                return None
            task = {
                **task,
                "judge_role": role,
                "skill_instructions": skill_instructions(role),
            }
            confidence = {}
            runtime = replace(self.runtime, case_confidence_sink=confidence.update)
            output = runtime.run(
                task=task,
                stage_key=f"eligibility_decisions_{context.case.case_id}_{role}",
                stage_label=f"Decision eligibility {role} judge",
                paper_id=paper_id,
                workspace_id=workspace_id,
                api_key=self.api_key,
            )
            validate_output(output, task, ids)
            alias = aliases[context.case.case_id]
            assessment = next(
                item for item in output.assessments if item.case_ref == alias
            )
            vote = eligibility_adjudication_vote_from_assessment(
                case_id=context.case.case_id,
                judge_role=role,
                assessment=assessment,
                candidate_id_by_ref=ids[alias],
                model=runtime.model,
            )
            return vote, confidence.get(alias, 0.0)

        votes = {}
        errors: set[str] = set()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            pending = [
                (context, role, pool.submit(copy_context().run, probe, context, role))
                for context in contexts
                for role in ("negative", "positive")
            ]
            for context, role, future in pending:
                try:
                    votes[context.case.case_id, role] = future.result()
                except Exception:
                    # The adapter already records usage/errors. A provider error
                    # cannot silently turn a claim into a rejection or a zero.
                    errors.add(context.case.case_id)

        eligible = []
        for context in contexts:
            case_id = context.case.case_id
            negative, positive = (
                votes.get((case_id, role)) for role in ("negative", "positive")
            )
            if (
                case_id in errors
                or negative is None
                or positive is None
                or negative[0].technical_unresolved
                or positive[0].technical_unresolved
                or negative[0].selected_candidate_id
                != positive[0].selected_candidate_id
            ):
                continue
            confidence = min(negative[1], positive[1])
            if confidence <= 0 or (
                self.mode == "confidence_threshold" and confidence < self.cutoff
            ):
                continue
            eligible.append((confidence, case_id, context, negative[0], positive[0]))
        eligible.sort(key=lambda row: (-row[0], row[1]))
        if self.mode == "top_fraction":
            eligible = eligible[: math.floor(self.cutoff * len(contexts))]
        selected = {
            case_id: decide_eligibility_adjudication(
                case_id=case_id,
                candidate_ids=[c.candidate_id for c in context.candidates],
                negative_vote=negative,
                positive_vote=positive,
            )
            for _, case_id, context, negative, positive in eligible
        }
        return selected, {
            "mode": self.mode,
            "cutoff": self.cutoff,
            "scope": "paper",
            "case_count": len(contexts),
            "decision_model": self.runtime.model,
            "accepted_cases": [
                {"case_id": row[1], "confidence": row[0]} for row in eligible
            ],
            "full_judge_case_ids": [
                c.case.case_id for c in contexts if c.case.case_id not in selected
            ],
            "decision_error_case_ids": sorted(errors),
        }
