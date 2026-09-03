from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, create_model

from miner.agent_v1.provider import dspy_model_id, normalize_provider
from miner.agent_v1.runtime.usage import empty_usage, usage_from_dspy_lm

from .model_usage import UsageSink
from .eligibility import (
    ELIGIBILITY_GATES,
    BlindEligibilityDiscoveryOutput,
    BlindEligibilityFinding,
    EligibilityAgentOutput,
    EligibilityCandidateAssessment,
    EligibilityGateAssessment,
    EligibilityTiebreakAssessment,
    EligibilityTiebreakOutput,
)


@dataclass
class DSPyEligibilityRuntime:
    provider: str
    api_base: str
    api_key_env: str
    temperature: float = 0.0
    max_tokens: int = 32768
    timeout_seconds: float = 300.0
    usage_sink: UsageSink | None = None
    raw_output_sink: Callable[[str], None] | None = field(default=None, repr=False)
    dspy_module: Any | None = field(default=None, repr=False)
    program: Callable[..., Any] | None = field(default=None, repr=False)

    def run(
        self,
        *,
        task: dict[str, Any],
        output_model: type[BaseModel],
        model: str,
        stage_key: str,
        stage_label: str,
        paper_id: str,
        workspace_id: str,
        validator: Callable[[BaseModel], None] | None = None,
    ) -> BaseModel:
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("dspy_program_unavailable")
        status = "success"
        error: str | None = None
        lm = None
        try:
            program = self.program
            schema_model = output_model
            if program is None:
                dspy_module = self._dspy()
                api_key = os.getenv(self.api_key_env, "").strip()
                if not api_key:
                    raise RuntimeError(
                        f"{self.api_key_env} is required for DSPy eligibility adjudication."
                    )
                lm = dspy_module.LM(
                    model=dspy_model_id(
                        model,
                        provider=self.provider,
                        api_base=self.api_base,
                    ),
                    api_key=api_key,
                    api_base=self.api_base,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout_seconds,
                    num_retries=0,
                )
                schema_model = _constrained_output_model(output_model, task)
                program = self._program(dspy_module, schema_model)
                if hasattr(dspy_module, "context"):
                    with dspy_module.context(lm=lm):
                        prediction = program(**_program_inputs(task, schema_model))
                else:  # pragma: no cover - retained for older DSPy versions
                    dspy_module.configure(lm=lm)
                    prediction = program(**_program_inputs(task, schema_model))
            else:
                prediction = program(**_program_inputs(task, schema_model))
            predicted_output = getattr(prediction, "eligibility", None)
            if predicted_output is None:
                predicted_output = getattr(
                    prediction,
                    "eligibility_json",
                    prediction if isinstance(prediction, (str, dict)) else "",
                )
            if isinstance(predicted_output, BaseModel):
                raw = predicted_output.model_dump_json()
                payload = output_model.model_validate(
                    predicted_output.model_dump(mode="json")
                )
            elif isinstance(predicted_output, dict):
                raw = json.dumps(predicted_output, ensure_ascii=False)
                payload = output_model.model_validate(predicted_output)
            else:
                raw = str(predicted_output)
                payload = output_model.model_validate(_parse_json_object(raw))
            if self.raw_output_sink is not None:
                self.raw_output_sink(raw)
            if validator is not None:
                validator(payload)
            return payload
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if lm is not None:
                usage = usage_from_dspy_lm(lm)
                _close_dspy_lm(lm)
            if self.usage_sink is not None:
                self.usage_sink(
                    {
                        "paper_id": paper_id,
                        "stage_key": f"silver_{stage_key}",
                        "stage_label": stage_label,
                        "role": "validator",
                        "operation_id": f"{workspace_id}:{stage_key}",
                        "harness": "dspy",
                        "runtime": "dspy-predict",
                        "provider": normalize_provider(self.provider, api_base=self.api_base),
                        "model": model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "workflow": "standalone_eligibility",
                            "workspace_id": workspace_id,
                        },
                    }
                )

    def _dspy(self):
        if self.dspy_module is not None:
            return self.dspy_module
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover - depends on local install
            raise RuntimeError("dspy is required for DSPy eligibility adjudication.") from exc
        self.dspy_module = dspy_module
        return dspy_module

    @staticmethod
    def _program(dspy_module, output_model: type[BaseModel]):
        EligibilitySignature = type(
            "EligibilitySignature",
            (dspy_module.Signature,),
            {
                "__doc__": (
                    "Apply all Claims eligibility gates and obey the supplied JSON contract "
                    "exactly. Cite only source-span identifiers that appear verbatim as keys "
                    "in task_json. Never invent, shorten, renumber, or normalize an identifier."
                ),
                "__annotations__": {
                    "task_json": str,
                    "required_json_schema": str,
                    "eligibility": output_model,
                },
                "task_json": dspy_module.InputField(),
                "required_json_schema": dspy_module.InputField(),
                "eligibility": dspy_module.OutputField(
                    desc="One typed object matching required_json_schema."
                ),
            },
        )

        return dspy_module.Predict(EligibilitySignature)


def _program_inputs(task: dict[str, Any], output_model: type[BaseModel]) -> dict[str, str]:
    return {
        "task_json": json.dumps(task, ensure_ascii=False, sort_keys=True),
        "required_json_schema": json.dumps(
            output_model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def _constrained_output_model(
    output_model: type[BaseModel],
    task: dict[str, Any],
) -> type[BaseModel]:
    source_refs = tuple(sorted(str(ref) for ref in dict(task.get("source_spans") or {})))
    candidates = task.get("candidates")
    candidate_refs = tuple(
        sorted(
            str(candidate.get("candidate_ref"))
            for candidate in (candidates if isinstance(candidates, list) else [])
            if isinstance(candidate, dict) and candidate.get("candidate_ref")
        )
    )
    locked = task.get("locked_independent_findings")
    finding_rows = locked.get("findings") if isinstance(locked, dict) else []
    finding_refs = tuple(
        sorted(
            str(finding.get("finding_ref"))
            for finding in (finding_rows if isinstance(finding_rows, list) else [])
            if isinstance(finding, dict) and finding.get("finding_ref")
        )
    )
    suffix = sha256(
        repr((output_model.__name__, source_refs, candidate_refs, finding_refs)).encode(
            "utf-8"
        )
    ).hexdigest()[:10]
    span_type = _literal_type(source_refs)
    candidate_type = _literal_type(candidate_refs)
    finding_type = _literal_type(finding_refs)
    gate_model = create_model(
        f"DSPyEligibilityGate_{suffix}",
        __base__=EligibilityGateAssessment,
        cited_span_ids=(list[span_type], Field(default_factory=list)),
    )
    assessment_model = create_model(
        f"DSPyEligibilityAssessment_{suffix}",
        __base__=EligibilityCandidateAssessment,
        candidate_ref=(candidate_type, ...),
        gates=(list[gate_model], Field(min_length=len(ELIGIBILITY_GATES))),
    )
    if issubclass(output_model, EligibilityTiebreakOutput):
        tiebreak_model = create_model(
            f"DSPyEligibilityTiebreakAssessment_{suffix}",
            __base__=EligibilityTiebreakAssessment,
            candidate_ref=(candidate_type, ...),
            gates=(list[gate_model], Field(min_length=len(ELIGIBILITY_GATES))),
            matched_finding_refs=(list[finding_type], Field(default_factory=list)),
        )
        return create_model(
            f"DSPyEligibilityTiebreakOutput_{suffix}",
            __base__=EligibilityTiebreakOutput,
            assessments=(list[tiebreak_model], ...),
        )
    if issubclass(output_model, BlindEligibilityDiscoveryOutput):
        finding_model = create_model(
            f"DSPyBlindEligibilityFinding_{suffix}",
            __base__=BlindEligibilityFinding,
            cited_span_ids=(list[span_type], Field(min_length=1)),
        )
        return create_model(
            f"DSPyBlindEligibilityDiscoveryOutput_{suffix}",
            __base__=BlindEligibilityDiscoveryOutput,
            findings=(list[finding_model], Field(default_factory=list)),
        )
    if issubclass(output_model, EligibilityAgentOutput):
        return create_model(
            f"DSPyEligibilityAgentOutput_{suffix}",
            __base__=EligibilityAgentOutput,
            assessments=(list[assessment_model], ...),
        )
    return output_model


def _literal_type(values: tuple[str, ...]):
    return Literal.__getitem__(values) if values else str


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"DSPy eligibility returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("DSPy eligibility must return one JSON object.")
    return payload


def _close_dspy_lm(lm: Any) -> None:
    for target in (lm, getattr(lm, "client", None)):
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                continue
