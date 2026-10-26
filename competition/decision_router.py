"""Temporary Decisions API router for single-candidate early-stage cases.

The model and threshold belong in the frozen weekly configuration. Pair cases
and uncertain singleton cases continue through the full adjudication judges.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from validator.agent_v1.adjudication_models import (
    AdjudicationConsensus, AdjudicationContextBundle, AdjudicationVote,
)


QUESTION = (
    "Which candidate is fully supported by the paper's own evidence and faithful "
    "to its population, conditions, direction, and qualifications? Select neither "
    "when the candidate is unsupported or materially overstates the source."
)
PROMPT_SHA256 = hashlib.sha256(QUESTION.encode()).hexdigest()


@dataclass(frozen=True)
class OpenAIDecisionsRouter:
    api_key: str = field(repr=False)
    threshold: float
    model: str = "gpt-6-luna"
    api_base: str = "https://api.openai.com/v1"
    timeout_seconds: float = 120.0
    http_post: Callable[..., Any] = requests.post

    def __post_init__(self) -> None:
        if not self.api_key or not 0.5 <= self.threshold <= 1:
            raise ValueError("Decisions router needs a key and a threshold in [0.5, 1]")
        if self.model != "gpt-6-luna":
            raise ValueError("OpenAI Decisions currently requires the frozen gpt-6-luna model")

    def __call__(self, context: AdjudicationContextBundle) -> AdjudicationConsensus | None:
        case = context.case
        if len(case.candidate_ids) != 1 or len(context.candidates) != 1:
            return None
        candidate = context.candidates[0]
        if candidate.candidate_id != case.candidate_ids[0]:
            raise ValueError("Decisions candidate differs from the adjudication case")
        if not context.source_context.strip():
            return None
        payload = {
            "model": self.model,
            "input": json.dumps({
                "case_question": case.question,
                "candidate": {
                    "id": candidate.candidate_id, "statement": candidate.statement,
                    "source_quotes": candidate.source_quotes,
                },
                "source_context": context.source_context[:30000],
            }, sort_keys=True, ensure_ascii=False),
            "questions": [{
                "type": "choice", "name": "supported_candidate",
                "instructions": QUESTION,
                "choices": [
                    {"value": candidate.candidate_id,
                     "description": "The complete candidate is supported by this paper."},
                    {"value": "neither",
                     "description": "The candidate is unsupported or materially inaccurate."},
                ],
            }],
        }
        response = self.http_post(
            f"{self.api_base.rstrip('/')}/decisions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, list) or len(answers) != 1:
            raise ValueError("Decisions response has no unique answer")
        answer = answers[0]
        if answer.get("type") == "refusal" and answer.get("name") == "supported_candidate":
            return None
        if answer.get("type") != "choice" or answer.get("name") != "supported_candidate":
            raise ValueError("Decisions response has an unexpected answer type")
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if choice not in {candidate.candidate_id, "neither"} or not isinstance(probabilities, list):
            raise ValueError("Decisions response has an invalid choice")
        by_value = {row["value"]: row["probability"] for row in probabilities}
        if (set(by_value) != {candidate.candidate_id, "neither"}
            or any(not isinstance(value, (int, float)) or not 0 <= value <= 1
                   for value in by_value.values())
            or abs(sum(by_value.values()) - 1) > 0.02):
            raise ValueError("Decisions response has invalid probabilities")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("Decisions response has invalid confidence")
        if confidence < self.threshold:
            return None
        if candidate.origin == "bronze":
            disposition = "miner_error" if choice == candidate.candidate_id else "reference_error"
        else:
            disposition = "accepted_improvement" if choice == candidate.candidate_id else "miner_error"
        vote = AdjudicationVote(
            case_id=case.case_id, pass_id="openai-decisions", adjudication_profile_id=PROMPT_SHA256,
            model_runtime_id=self.model, candidate_order=[candidate.candidate_id],
            disposition=disposition, confidence=float(confidence),
            rationale="High-confidence direct choice from frozen Decisions configuration.",
        )
        return AdjudicationConsensus(
            case_id=case.case_id, votes=[vote], final_disposition=disposition,
            final_confidence=float(confidence), agreement_rate=1.0, route="decision_model",
            rationale=vote.rationale,
        )
