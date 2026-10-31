"""Frozen stage-specific Silver evaluator for the trusted controller."""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from validator.agent_v1.adjudication_runner import AdjudicationPass

from validator.agent_v1.eligibility_decisions import PROMPT_SHA256, InputTokenRateLimiter
from .validator_workflow import build_validator_workflow, validated_workflow_spec
from .gold_judge import judge_known_answer_case
from .pair_evaluator import WeeklyPaperEvaluation, evaluate_weekly_paper
from .publication import canonical_sha256
from .policy_runtime import policy_runtime_config
from .silver_runtime import silver_runtime_config


def decision_routing_config(*, provider, model, api_base, prescreen_fraction, screen_threshold):
    return {
        "schema_version": "claims_competition_eligibility_routing_v2",
        "provider": provider, "model": model, "api_base": api_base,
        "prompt_sha256": PROMPT_SHA256, "routing_scope": "paper", "predicate_threshold": 0.5,
        "prescreen_acceptance_fraction": prescreen_fraction,
        "screen_confidence_threshold": screen_threshold,
    }


def _pass_descriptor(adjudication_pass: AdjudicationPass) -> dict[str, Any]:
    """Public, credential-free identity of the configured judge runtime."""
    fields = ("pass_id", "adjudication_profile_id", "model_runtime_id", "model",
              "api_base", "temperature", "max_tokens", "timeout_seconds",
              "num_retries", "prompt_mode", "provider_slug")
    descriptor = {
        "implementation": (type(adjudication_pass).__module__ + "."
                           + type(adjudication_pass).__qualname__),
    }
    for key in fields:
        value = getattr(adjudication_pass, key, None)
        if value is not None:
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("judge runtime descriptor contains an unsupported value")
            descriptor[key] = value
    if not all(descriptor.get(key) for key in fields[:3]):
        raise ValueError("judge pass has no stable runtime identity")
    return descriptor


def judge_runtime_config(
    panels: Mapping[str, tuple[AdjudicationPass, AdjudicationPass]],
    tiebreak_pass: AdjudicationPass | None,
) -> dict[str, Any]:
    """Descriptor to include in the hashed weekly rules before intake."""
    if set(panels) != {"J1", "J2"} or any(len(passes) != 2 for passes in panels.values()):
        raise ValueError("J1 and J2 each need two passes")
    return {
        "judge_panels": {
            judge_id: [_pass_descriptor(p) for p in panels[judge_id]]
            for judge_id in ("J1", "J2")
        },
        "tiebreak_pass": _pass_descriptor(tiebreak_pass) if tiebreak_pass else None,
    }


@dataclass(frozen=True)
class FrozenEvaluationPolicy:
    round_id: str
    configuration_sha256: str
    configuration: Mapping[str, Any]
    adjudication_passes_by_judge: Mapping[str, tuple[AdjudicationPass, AdjudicationPass]] = field(repr=False)
    source_context_for_paper: Callable[[str], Mapping[str, str]]
    decision_api_key: str = field(repr=False)
    tiebreak_pass: AdjudicationPass | None = field(default=None, repr=False)
    prescreen_fraction: float = 0.8
    screen_threshold: float = 0.9
    decision_api_base: str = "https://api.openai.com/v1"
    decision_provider: str = "openai"
    decision_model: str = "gpt-6-luna"
    decision_http_post: Callable | None = field(default=None, repr=False)
    decision_usage_sink: Callable | None = field(default=None, repr=False)
    judge_request_gate: Any = field(default=None, repr=False)
    decision_rate_limiter: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (not self.round_id or not re.fullmatch(r"[0-9a-f]{64}", self.configuration_sha256)
            or set(self.adjudication_passes_by_judge) != {"J1", "J2"}
            or any(len(passes) != 2 for passes in self.adjudication_passes_by_judge.values())
            or not 0 <= self.prescreen_fraction <= 1
            or not 0.5 <= self.screen_threshold <= 1):
            raise ValueError("weekly evaluation policy is incomplete")
        self._verify_frozen_runtime()
        if self.judge_request_gate is None:
            object.__setattr__(self, "judge_request_gate", threading.BoundedSemaphore(
                (self.configuration.get("judge_specs") or {}).get("max_in_flight", 16),
            ))
        if self.decision_rate_limiter is None:
            rate = self.configuration["silver_runtime"]["settings"]["CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_TOKENS_PER_SECOND"]
            object.__setattr__(self, "decision_rate_limiter", InputTokenRateLimiter(int(rate)))

    def _verify_frozen_runtime(self) -> None:
        frozen = judge_runtime_config(self.adjudication_passes_by_judge, self.tiebreak_pass)
        panels = frozen["judge_panels"]
        silver = silver_runtime_config()
        validated_workflow_spec(self.configuration.get("workflow_specs"))
        if "workflow_specs" not in self.configuration:
            raise ValueError("frozen validator workflow spec is missing")
        embedding_mode = silver["settings"]["CLAIMS_SILVER_PAIRING_EMBEDDING_MODE"].strip().lower()
        if (embedding_mode in {"openrouter", "on", "true", "1"}
            and not silver["embedding_credential_present"]):
            raise ValueError("enabled Silver embeddings have no trusted controller credential")
        if (canonical_sha256(self.configuration) != self.configuration_sha256
            or self.configuration.get("decision_routing") != self.public_routing_config()
            or self.configuration.get("silver_runtime") != silver
            or self.configuration.get("competition_policy_runtime") != policy_runtime_config()
            or any(self.configuration.get(key) != value for key, value in frozen.items())):
            raise ValueError("Silver runtime, judge runtime or Decision routing differs from frozen weekly configuration")
        def model_signature(judge_id: str) -> tuple[tuple[Any, Any, Any], ...]:
            return tuple((row.get("model"), row["adjudication_profile_id"],
                          row["model_runtime_id"]) for row in panels[judge_id])

        if model_signature("J1") == model_signature("J2"):
            raise ValueError("J1 and J2 must be distinct frozen judge configurations")

    def public_routing_config(self) -> dict[str, object]:
        """Fields to bind into the round's frozen configuration before intake."""
        return decision_routing_config(
            provider=self.decision_provider, model=self.decision_model, api_base=self.decision_api_base,
            prescreen_fraction=self.prescreen_fraction, screen_threshold=self.screen_threshold,
        )

    def public_judge_config(self) -> dict[str, Any]:
        return judge_runtime_config(self.adjudication_passes_by_judge, self.tiebreak_pass)

    def judge_known_answer(self, sealed_case: Mapping[str, Any]) -> dict[str, str]:
        self._verify_frozen_runtime()
        if self.tiebreak_pass is None:
            raise ValueError("known-answer audit needs the frozen tiebreak judge")
        return judge_known_answer_case(sealed_case, tiebreak_pass=self.tiebreak_pass)

    def evaluate_for_stage(self, stage: str) -> Callable[..., WeeklyPaperEvaluation]:
        if stage not in {"prescreen", "screen", "final", "week_one"}:
            raise ValueError("unknown evaluation stage")
        if stage in {"prescreen", "screen"}:
            if not self.decision_api_key:
                raise ValueError("early-stage Decisions API credential is required")

        def evaluate(**kwargs) -> WeeklyPaperEvaluation:
            self._verify_frozen_runtime()
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
                file_agent_workflow=build_validator_workflow(
                    self, stage=stage, judge_id=kwargs.get("judge_id", "J1"),
                    decision_max_workers=kwargs.pop("decision_max_workers", 1),
                ),
                **kwargs, silver_record_id=silver_record_id,
                adjudication_passes_by_judge=self.adjudication_passes_by_judge,
                tiebreak_pass=self.tiebreak_pass,
                source_context_by_span_id=dict(context),
            )

        return evaluate
