"""Frozen stage-specific Silver evaluator for the trusted controller."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from validator.agent_v1.adjudication_runner import AdjudicationPass

from .decision_router import OpenAIDecisionsRouter, PROMPT_SHA256
from .gold_judge import judge_known_answer_case
from .pair_evaluator import WeeklyPaperEvaluation, evaluate_weekly_paper
from .publication import canonical_sha256


@dataclass(frozen=True)
class FrozenEvaluationPolicy:
    round_id: str
    configuration_sha256: str
    configuration: Mapping[str, Any]
    adjudication_passes_by_judge: Mapping[str, tuple[AdjudicationPass, AdjudicationPass]]
    source_context_for_paper: Callable[[str], Mapping[str, str]]
    decision_api_key: str = field(repr=False)
    tiebreak_pass: AdjudicationPass | None = None
    prescreen_threshold: float = 0.8
    screen_threshold: float = 0.9
    decision_api_base: str = "https://api.openai.com/v1"
    decision_http_post: Callable | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (not self.round_id or not re.fullmatch(r"[0-9a-f]{64}", self.configuration_sha256)
            or set(self.adjudication_passes_by_judge) != {"J1", "J2"}
            or any(len(passes) != 2 for passes in self.adjudication_passes_by_judge.values())
            or not 0.5 <= self.prescreen_threshold <= 1
            or not 0.5 <= self.screen_threshold <= 1):
            raise ValueError("weekly evaluation policy is incomplete")
        if (canonical_sha256(self.configuration) != self.configuration_sha256
            or self.configuration.get("decision_routing") != self.public_routing_config()):
            raise ValueError("Decision routing differs from frozen weekly configuration")

    def public_routing_config(self) -> dict[str, object]:
        """Fields to bind into the round's frozen configuration before intake."""
        return {
            "provider": "openai", "model": "gpt-6-luna", "prompt_sha256": PROMPT_SHA256,
            "prescreen_confidence_threshold": self.prescreen_threshold,
            "screen_confidence_threshold": self.screen_threshold,
        }

    def judge_known_answer(self, sealed_case: Mapping[str, Any]) -> dict[str, str]:
        if self.tiebreak_pass is None:
            raise ValueError("known-answer audit needs the frozen tiebreak judge")
        return judge_known_answer_case(sealed_case, tiebreak_pass=self.tiebreak_pass)

    def evaluate_for_stage(self, stage: str) -> Callable[..., WeeklyPaperEvaluation]:
        if stage not in {"prescreen", "screen", "final", "week_one"}:
            raise ValueError("unknown evaluation stage")
        router = None
        if stage in {"prescreen", "screen"}:
            if not self.decision_api_key:
                raise ValueError("early-stage Decisions API credential is required")
            kwargs = {"http_post": self.decision_http_post} if self.decision_http_post else {}
            router = OpenAIDecisionsRouter(
                self.decision_api_key,
                self.prescreen_threshold if stage == "prescreen" else self.screen_threshold,
                api_base=self.decision_api_base, **kwargs,
            )

        def evaluate(**kwargs) -> WeeklyPaperEvaluation:
            if kwargs.get("stage") != stage:
                raise ValueError("evaluation stage differs from frozen policy")
            paper_id = kwargs.get("paper_id")
            if not isinstance(paper_id, str) or not paper_id:
                raise ValueError("paper ID is required for frozen evaluation")
            context = self.source_context_for_paper(paper_id)
            if not isinstance(context, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in context.items()
            ):
                raise ValueError("paper source context is malformed")
            silver_record_id = "competition_silver_" + hashlib.sha256(
                "\0".join((self.round_id, stage, paper_id,
                           self.configuration_sha256)).encode(),
            ).hexdigest()[:32]
            return evaluate_weekly_paper(
                **kwargs, silver_record_id=silver_record_id,
                adjudication_passes_by_judge=self.adjudication_passes_by_judge,
                tiebreak_pass=self.tiebreak_pass,
                source_context_by_span_id=dict(context), decision_router=router,
            )

        return evaluate
