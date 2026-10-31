from __future__ import annotations

import json
import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .eligibility import (
    ELIGIBILITY_GATES,
    EligibilityAdjudicationAgentOutput,
    EligibilityAdjudicationCaseAssessment,
    EligibilityCandidateAssessment,
    EligibilityClaimAtomAssessment,
    EligibilityGateAssessment,
)
from .model_usage import UsageSink, provider_from_model_or_base


_GATE_INSTRUCTIONS = {
    "propositional_completeness": (
        "Is the exact candidate a complete, evaluable scientific proposition rather "
        "than a fragment, label, topic, or underspecified assertion?"
    ),
    "author_assertion": (
        "Does the paper itself assert the exact candidate, rather than merely mention, "
        "cite, hypothesize, or describe it as background?"
    ),
    "paper_original_support": (
        "Do the candidate's linked source spans provide direct support generated or "
        "analyzed by this paper for every material part of the exact candidate?"
    ),
    "argument_sufficiency": (
        "Do the linked evidence and reasoning support the candidate without an omitted "
        "material inferential step or unsupported component?"
    ),
    "fidelity": (
        "Is the exact candidate faithful to the paper, including direction, population, "
        "conditions, modality, uncertainty, and material qualifications?"
    ),
    "author_marked_salience": (
        "Is the candidate presented by the authors as a meaningful paper finding rather "
        "than an incidental observation, method detail, or peripheral fact?"
    ),
}

# Freeze the reused request/projection contract, including predicate prompts.
PROMPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class DecisionsEligibilityError(RuntimeError):
    pass


@dataclass
class InputTokenRateLimiter:
    tokens_per_second: int
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    _next_available_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def acquire(self, estimated_tokens: int) -> None:
        if self.tokens_per_second <= 0 or estimated_tokens <= 0:
            return
        duration = estimated_tokens / self.tokens_per_second
        with self._lock:
            now = self.monotonic()
            start_at = max(now, self._next_available_at)
            self._next_available_at = start_at + duration
        delay = start_at - now
        if delay > 0:
            self.sleep(delay)


@dataclass
class DecisionsEligibilityRuntime:
    transport: str = "openai"
    api_base: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-6-luna"
    threshold: float = 0.5
    timeout_seconds: float = 120.0
    input_cost_per_million_usd: float | None = 0.10
    usage_sink: UsageSink | None = None
    rate_limiter: InputTokenRateLimiter | None = field(default=None, repr=False)
    raw_output_sink: Callable[[str], None] | None = field(default=None, repr=False)
    case_confidence_sink: Callable[[dict[str, float]], None] | None = field(
        default=None, repr=False
    )
    http_post: Callable[..., Any] = field(default=requests.post, repr=False)

    def run(
        self,
        *,
        task: dict[str, Any],
        stage_key: str,
        stage_label: str,
        paper_id: str,
        workspace_id: str,
        api_key: str,
    ) -> EligibilityAdjudicationAgentOutput:
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        status = "success"
        error: str | None = None
        usage = _empty_usage()
        canonical_request, question_map = build_decisions_request(
            task,
            model=self.model,
        )
        request_payload = request_for_transport(
            canonical_request,
            transport=self.transport,
        )
        estimated_input_tokens = estimate_input_tokens(request_payload)
        response_payload: dict[str, Any] | None = None
        try:
            if not api_key.strip():
                raise DecisionsEligibilityError(
                    f"{self.api_key_env} is required for Decisions adjudication."
                )
            if self.rate_limiter is not None:
                self.rate_limiter.acquire(estimated_input_tokens)
            response = self.http_post(
                decisions_endpoint(self.api_base, transport=self.transport),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=self.timeout_seconds,
            )
            if not 200 <= int(response.status_code) < 300:
                body = str(getattr(response, "text", ""))[:1000]
                raise DecisionsEligibilityError(
                    f"Decisions API returned HTTP {response.status_code}: {body}"
                )
            raw_payload = response.json()
            if not isinstance(raw_payload, dict):
                raise DecisionsEligibilityError(
                    "Decisions API returned a non-object response."
                )
            response_payload = raw_payload
            if self.raw_output_sink is not None:
                self.raw_output_sink(json.dumps(raw_payload, ensure_ascii=False))
            usage = _usage_from_response(
                raw_payload,
                input_cost_per_million_usd=self.input_cost_per_million_usd,
            )
            canonical_response = response_for_canonical_contract(
                raw_payload,
                transport=self.transport,
            )
            output = decisions_response_to_output(
                task,
                canonical_response,
                question_map=question_map,
                threshold=self.threshold,
            )
            if self.case_confidence_sink is not None:
                self.case_confidence_sink(
                    decisions_case_confidences(
                        canonical_response,
                        question_map=question_map,
                    )
                )
            return output
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if response_payload is not None and usage.get("prompt_tokens") is None:
                usage = _usage_from_response(
                    response_payload,
                    input_cost_per_million_usd=self.input_cost_per_million_usd,
                )
            if self.usage_sink is not None:
                self.usage_sink(
                    {
                        "paper_id": paper_id,
                        "stage_key": f"silver_{stage_key}",
                        "stage_label": stage_label,
                        "role": "validator",
                        "operation_id": f"{workspace_id}:{stage_key}",
                        "harness": "decisions",
                        "runtime": decisions_runtime_id(self.transport),
                        "provider": decisions_provider(
                            self.transport,
                            model=self.model,
                            api_base=self.api_base,
                        ),
                        "model": self.model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "workflow": "eligibility_adjudication",
                            "workspace_id": workspace_id,
                            "question_count": len(request_payload["questions"]),
                            "predicate_threshold": self.threshold,
                            "transport": self.transport,
                            "estimated_input_tokens": estimated_input_tokens,
                        },
                    }
                )


def normalize_decisions_transport(value: str) -> str:
    normalized = str(value or "openai").strip().lower().replace("_", "-")
    if normalized in {"openai", "openai-decisions"}:
        return "openai"
    if normalized in {"jev", "typesafe", "typesafe-jev", "systemone"}:
        return "jev"
    raise DecisionsEligibilityError("Decisions transport must be openai or jev.")


def decisions_endpoint(api_base: str, *, transport: str) -> str:
    suffix = (
        "systemone"
        if normalize_decisions_transport(transport) == "jev"
        else "decisions"
    )
    return f"{api_base.rstrip('/')}/{suffix}"


def decisions_runtime_id(transport: str) -> str:
    return (
        "typesafe-jev"
        if normalize_decisions_transport(transport) == "jev"
        else "openai-decisions"
    )


def decisions_provider(transport: str, *, model: str, api_base: str) -> str:
    if normalize_decisions_transport(transport) == "jev":
        return "typesafe"
    return provider_from_model_or_base(model, api_base)


def request_for_transport(
    canonical_request: dict[str, Any],
    *,
    transport: str,
) -> dict[str, Any]:
    if normalize_decisions_transport(transport) == "openai":
        return canonical_request
    try:
        state = json.loads(str(canonical_request["input"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionsEligibilityError(
            "Canonical Decisions request is missing valid shared input."
        ) from exc
    questions: dict[str, dict[str, Any]] = {}
    for question in canonical_request.get("questions", []):
        if not isinstance(question, dict):
            raise DecisionsEligibilityError(
                "Canonical Decisions request contains an invalid question."
            )
        name = str(question.get("name") or "").strip()
        question_type = question.get("type")
        if not name:
            raise DecisionsEligibilityError(
                "Canonical Decisions question is missing its name."
            )
        if question_type == "predicate":
            questions[name] = {
                "type": "noul",
                "instructions": question.get("instructions"),
                "criteria": {
                    "true": "The condition in the instructions is fully satisfied.",
                    "false": "The condition is not fully satisfied or the evidence is uncertain.",
                },
            }
        elif question_type == "choice":
            criteria = {
                str(choice.get("value")): choice.get("description")
                for choice in question.get("choices", [])
                if isinstance(choice, dict) and str(choice.get("value") or "").strip()
            }
            if len(criteria) < 2:
                raise DecisionsEligibilityError(
                    f"Choice question {name} requires at least two choices."
                )
            questions[name] = {
                "type": "choice",
                "instructions": question.get("instructions"),
                "criteria": criteria,
            }
        else:
            raise DecisionsEligibilityError(
                f"Unsupported canonical Decisions question type: {question_type}."
            )
    return {
        "model": canonical_request.get("model"),
        "state": state,
        "questions": questions,
    }


def response_for_canonical_contract(
    response: dict[str, Any],
    *,
    transport: str,
) -> dict[str, Any]:
    if normalize_decisions_transport(transport) == "openai":
        return response
    raw_answers = response.get("answers")
    if not isinstance(raw_answers, dict):
        raise DecisionsEligibilityError("Jev response is missing named answers.")
    answers: list[dict[str, Any]] = []
    for name, answer in raw_answers.items():
        if not isinstance(answer, dict):
            raise DecisionsEligibilityError(f"Jev answer {name} is invalid.")
        if answer.get("type") == "noul":
            answers.append(
                {
                    "type": "predicate",
                    "name": str(name),
                    "probability": answer.get("noul"),
                }
            )
        elif answer.get("type") == "choice":
            probabilities = answer.get("probabilities")
            answers.append(
                {
                    "type": "choice",
                    "name": str(name),
                    "choice": answer.get("choice"),
                    "confidence": answer.get("confidence"),
                    "probabilities": [
                        {"value": value, "probability": probability}
                        for value, probability in probabilities.items()
                    ]
                    if isinstance(probabilities, dict)
                    else [],
                }
            )
        else:
            raise DecisionsEligibilityError(
                f"Jev answer {name} returned unsupported type {answer.get('type')}."
            )
    return {"answers": answers, "usage": response.get("usage", {})}


def estimate_input_tokens(payload: dict[str, Any]) -> int:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return max(1, (len(encoded) + 3) // 4)


def build_decisions_request(
    task: dict[str, Any],
    *,
    model: str,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    cases = task.get("cases")
    if not isinstance(cases, list) or not cases:
        raise DecisionsEligibilityError(
            "Decisions adjudication requires at least one case."
        )

    questions: list[dict[str, Any]] = []
    question_map: dict[str, dict[str, str]] = {}
    candidate_index = 0
    pair_index = 0
    for case in cases:
        if not isinstance(case, dict):
            raise DecisionsEligibilityError(
                "Decisions adjudication received an invalid case."
            )
        case_ref = str(case.get("case_ref") or "").strip()
        candidates = case.get("candidates")
        if (
            not case_ref
            or not isinstance(candidates, list)
            or len(candidates) not in {1, 2}
        ):
            raise DecisionsEligibilityError(
                "Decisions adjudication cases require a case_ref and one or two candidates."
            )
        candidate_refs: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise DecisionsEligibilityError(
                    "Decisions adjudication received an invalid candidate."
                )
            candidate_ref = str(candidate.get("candidate_ref") or "").strip()
            if not candidate_ref:
                raise DecisionsEligibilityError(
                    "Decisions candidate is missing candidate_ref."
                )
            candidate_refs.append(candidate_ref)
            atom_name = f"c{candidate_index:04d}_atom"
            question_map[atom_name] = {
                "kind": "atom",
                "case_ref": case_ref,
                "candidate_ref": candidate_ref,
            }
            questions.append(
                {
                    "type": "predicate",
                    "name": atom_name,
                    "instructions": (
                        f"For candidate {candidate_ref}, are all material assertions in its "
                        "exact wording directly supported by that candidate's linked source "
                        "spans? Return a high probability only if every material assertion is "
                        "supported; absent, ambiguous, implied, or background-only support is false."
                    ),
                }
            )
            for gate_index, gate in enumerate(ELIGIBILITY_GATES):
                name = f"c{candidate_index:04d}_g{gate_index}"
                question_map[name] = {
                    "kind": "gate",
                    "case_ref": case_ref,
                    "candidate_ref": candidate_ref,
                    "gate": gate,
                }
                questions.append(
                    {
                        "type": "predicate",
                        "name": name,
                        "instructions": (
                            f"For candidate {candidate_ref}, {_GATE_INSTRUCTIONS[gate]} "
                            "Apply the shared adjudication policy and inspect its linked spans "
                            "before answering."
                        ),
                    }
                )
            candidate_index += 1
        if len(candidate_refs) == 2:
            name = f"p{pair_index:04d}"
            question_map[name] = {
                "kind": "preference",
                "case_ref": case_ref,
            }
            questions.append(
                {
                    "type": "choice",
                    "name": name,
                    "instructions": (
                        "If both candidates independently pass every eligibility gate, which "
                        "is the more faithful, directly supported, complete, and salient Silver "
                        "representative? Choose neither when neither independently passes. Do "
                        "not rescue, merge, or rewrite either candidate."
                    ),
                    "choices": [
                        {
                            "value": candidate_ref,
                            "description": f"Select exact candidate {candidate_ref}.",
                        }
                        for candidate_ref in candidate_refs
                    ]
                    + [
                        {
                            "value": "none",
                            "description": "Neither candidate independently passes eligibility.",
                        }
                    ],
                }
            )
            pair_index += 1

    shared_input = {
        "purpose": "Claims Silver eligibility adjudication",
        "judge_role": task.get("judge_role", "independent"),
        "mode": task.get("mode", "eligibility_selection"),
        "eligibility_profile_id": task.get("eligibility_profile_id"),
        "skill_instructions": task.get("skill_instructions", ""),
        "paper": task.get("paper", {}),
        "hard_gates": task.get("hard_gates", list(ELIGIBILITY_GATES)),
        "requirements": task.get("requirements", {}),
        "cases": cases,
        "source_spans": task.get("source_spans", {}),
        "blind_reconstruction": task.get("blind_reconstruction"),
        "primary_assessments": task.get("primary_assessments"),
    }
    return (
        {
            "model": model,
            "input": json.dumps(shared_input, ensure_ascii=False, sort_keys=True),
            "questions": questions,
        },
        question_map,
    )


def decisions_response_to_output(
    task: dict[str, Any],
    response: dict[str, Any],
    *,
    question_map: dict[str, dict[str, str]],
    threshold: float,
) -> EligibilityAdjudicationAgentOutput:
    raw_answers = response.get("answers")
    if not isinstance(raw_answers, list):
        raise DecisionsEligibilityError("Decisions response is missing answers.")
    answers: dict[str, dict[str, Any]] = {}
    for answer in raw_answers:
        if not isinstance(answer, dict):
            raise DecisionsEligibilityError(
                "Decisions response contains an invalid answer."
            )
        name = str(answer.get("name") or "").strip()
        if not name or name in answers:
            raise DecisionsEligibilityError(
                "Decisions response contains a missing or duplicate name."
            )
        if answer.get("type") == "refusal":
            raise DecisionsEligibilityError(f"Decisions API refused question {name}.")
        answers[name] = answer
    missing = sorted(set(question_map).difference(answers))
    unexpected = sorted(set(answers).difference(question_map))
    if missing or unexpected:
        raise DecisionsEligibilityError(
            f"Decisions response question mismatch: missing={missing} unexpected={unexpected}."
        )

    predicate_values: dict[tuple[str, str, str], float] = {}
    preferences: dict[str, str] = {}
    preference_confidence: dict[str, float | None] = {}
    for name, metadata in question_map.items():
        answer = answers[name]
        kind = metadata["kind"]
        if kind in {"atom", "gate"}:
            if answer.get("type") != "predicate" or not isinstance(
                answer.get("probability"), int | float
            ):
                raise DecisionsEligibilityError(
                    f"Decisions question {name} did not return a predicate probability."
                )
            probability = float(answer["probability"])
            if not 0.0 <= probability <= 1.0:
                raise DecisionsEligibilityError(
                    f"Decisions question {name} returned an invalid probability."
                )
            key = (
                metadata["case_ref"],
                metadata["candidate_ref"],
                metadata.get("gate", "atom"),
            )
            predicate_values[key] = probability
        elif kind == "preference":
            if answer.get("type") != "choice":
                raise DecisionsEligibilityError(
                    f"Decisions question {name} did not return a choice."
                )
            preferences[metadata["case_ref"]] = str(answer.get("choice") or "")
            confidence = answer.get("confidence")
            preference_confidence[metadata["case_ref"]] = (
                float(confidence) if isinstance(confidence, int | float) else None
            )

    case_assessments: list[EligibilityAdjudicationCaseAssessment] = []
    for case in task.get("cases", []):
        case_ref = str(case["case_ref"])
        candidate_assessments: list[EligibilityCandidateAssessment] = []
        passing_refs: list[str] = []
        for candidate in case["candidates"]:
            candidate_ref = str(candidate["candidate_ref"])
            statement = str(candidate.get("statement") or candidate_ref)
            linked_span_ids = [
                str(span_id)
                for span_id in candidate.get("source_span_ids", [])
                if str(span_id)
            ]
            atom_probability = predicate_values[(case_ref, candidate_ref, "atom")]
            atom_supported = atom_probability >= threshold and bool(linked_span_ids)
            gates: list[EligibilityGateAssessment] = []
            gate_probabilities: dict[str, float] = {}
            for gate in ELIGIBILITY_GATES:
                probability = predicate_values[(case_ref, candidate_ref, gate)]
                passed = probability >= threshold
                if gate == "paper_original_support" and not linked_span_ids:
                    passed = False
                gate_probabilities[gate] = probability
                gates.append(
                    EligibilityGateAssessment(
                        gate=gate,
                        passed=passed,
                        cited_span_ids=(
                            linked_span_ids if gate == "paper_original_support" else []
                        ),
                        rationale=(
                            f"Decisions predicate probability={probability:.6f}; "
                            f"threshold={threshold:.6f}."
                        ),
                    )
                )
            candidate_passes = atom_supported and all(gate.passed for gate in gates)
            if candidate_passes:
                passing_refs.append(candidate_ref)
            candidate_assessments.append(
                EligibilityCandidateAssessment(
                    candidate_ref=candidate_ref,
                    claim_atoms=[statement],
                    claim_atom_assessments=[
                        EligibilityClaimAtomAssessment(
                            atom=statement,
                            supported=atom_supported,
                            cited_span_ids=(linked_span_ids if atom_supported else []),
                            rationale=(
                                f"Decisions whole-candidate support probability="
                                f"{atom_probability:.6f}; threshold={threshold:.6f}."
                            ),
                        )
                    ],
                    gates=gates,
                    reviewed_span_ids=linked_span_ids,
                    rationale=(
                        "Decisions API eligibility projection; "
                        f"candidate_passes={candidate_passes}; gate_probabilities="
                        f"{json.dumps(gate_probabilities, sort_keys=True)}."
                    ),
                )
            )
        if not passing_refs:
            selected_ref = None
            selection_reason = "Every candidate failed at least one predicate gate."
        elif len(passing_refs) == 1:
            selected_ref = passing_refs[0]
            selection_reason = "Exactly one candidate passed every predicate gate."
        else:
            selected_ref = preferences.get(case_ref)
            if selected_ref not in passing_refs:
                raise DecisionsEligibilityError(
                    f"Decisions preference for {case_ref} did not select a passing candidate."
                )
            confidence = preference_confidence.get(case_ref)
            selection_reason = (
                f"Both candidates passed; preference selected {selected_ref}"
                + (
                    f" with confidence={confidence:.6f}."
                    if confidence is not None
                    else "."
                )
            )
        case_assessments.append(
            EligibilityAdjudicationCaseAssessment(
                case_ref=case_ref,
                candidate_assessments=candidate_assessments,
                selected_candidate_ref=selected_ref,
                rationale=f"Decisions API adjudication. {selection_reason}",
            )
        )
    return EligibilityAdjudicationAgentOutput(assessments=case_assessments)


def decisions_case_confidences(
    response: dict[str, Any],
    *,
    question_map: dict[str, dict[str, str]],
) -> dict[str, float]:
    """Conservatively route on certainty, separately from predicate truth.

    A predicate at p=0.05 is a confident negative, not an uncertain positive.
    Every predicate (and a pair's preference) must be confident for a case
    to bypass the full judges. Missing confidence forces full judging.
    """
    import math

    answers = {answer["name"]: answer for answer in response["answers"]}
    result: dict[str, float] = {}
    for name, metadata in question_map.items():
        answer = answers[name]
        value = (
            answer.get("probability")
            if metadata["kind"] != "preference"
            else answer.get("confidence")
        )
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            confidence = 0.0
        else:
            confidence = (
                max(value, 1 - value) if metadata["kind"] != "preference" else value
            )
        case_ref = metadata["case_ref"]
        result[case_ref] = min(result.get(case_ref, 1.0), confidence)
    return result


def _usage_from_response(
    payload: dict[str, Any],
    *,
    input_cost_per_million_usd: float | None,
) -> dict[str, Any]:
    raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    input_tokens = _first_int(raw, "input_tokens", "prompt_tokens")
    output_tokens = _first_int(raw, "output_tokens", "completion_tokens")
    total_tokens = _first_int(raw, "total_tokens")
    if total_tokens is None:
        total_tokens = (
            input_tokens + (output_tokens or 0)
            if input_tokens is not None
            else output_tokens
        )
    reported_cost = _first_float(raw, "cost", "cost_usd", "actual_cost_usd")
    if reported_cost is not None:
        cost = reported_cost
        cost_kind = "actual"
        source = "decisions_api_response"
    elif input_tokens is not None and input_cost_per_million_usd is not None:
        cost = input_tokens * input_cost_per_million_usd / 1_000_000
        cost_kind = "estimated"
        source = "decisions_api_response+configured_pricing"
    else:
        cost = None
        cost_kind = "unavailable"
        source = "decisions_api_usage_unavailable"
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": (
            output_tokens
            if output_tokens is not None
            else (0 if input_tokens is not None else None)
        ),
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": total_tokens,
        "cost_usd": round(cost, 8) if cost is not None else None,
        "cost_kind": cost_kind,
        "source": source,
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "cost_kind": "unavailable",
        "source": "decisions_api_not_called",
    }


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _first_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None
