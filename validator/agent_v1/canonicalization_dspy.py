from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel

from miner.agent_v1.provider import dspy_model_id, normalize_provider
from miner.agent_v1.runtime.usage import empty_usage, usage_from_dspy_lm

from .model_usage import UsageSink


@dataclass
class DSPyCanonicalizationRuntime:
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
        schema_model: type[BaseModel] | None = None,
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
        effective_schema_model = schema_model or output_model
        try:
            program = self.program
            if program is None:
                dspy_module = self._dspy()
                api_key = os.getenv(self.api_key_env, "").strip()
                if not api_key:
                    raise RuntimeError(
                        f"{self.api_key_env} is required for DSPy canonicalization."
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
                program = self._program(dspy_module, effective_schema_model)
                if hasattr(dspy_module, "context"):
                    with dspy_module.context(lm=lm):
                        prediction = program(
                            **_program_inputs(task, effective_schema_model)
                        )
                else:  # pragma: no cover - retained for older DSPy versions
                    dspy_module.configure(lm=lm)
                    prediction = program(**_program_inputs(task, effective_schema_model))
            else:
                prediction = program(**_program_inputs(task, effective_schema_model))
            predicted_output = getattr(prediction, "canonicalization", None)
            if predicted_output is None:
                predicted_output = getattr(
                    prediction,
                    "canonicalization_json",
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
                        "provider": normalize_provider(
                            self.provider,
                            api_base=self.api_base,
                        ),
                        "model": model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "workflow": "canonicalization",
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
            raise RuntimeError("dspy is required for DSPy canonicalization.") from exc
        self.dspy_module = dspy_module
        return dspy_module

    @staticmethod
    def _program(dspy_module, output_model: type[BaseModel]):
        CanonicalizationSignature = type(
            "CanonicalizationSignature",
            (dspy_module.Signature,),
            {
                "__doc__": (
                    "Follow skill_instructions in task_json as the governing Claims "
                    "canonicalization policy. Review the complete paper-level candidate "
                    "set globally, obey every partition and evidence constraint, and return "
                    "one complete object matching required_json_schema. Use identifiers "
                    "exactly as supplied; never invent, shorten, or normalize them."
                ),
                "__annotations__": {
                    "task_json": str,
                    "required_json_schema": str,
                    "canonicalization": output_model,
                },
                "task_json": dspy_module.InputField(),
                "required_json_schema": dspy_module.InputField(),
                "canonicalization": dspy_module.OutputField(
                    desc="One typed object matching required_json_schema."
                ),
            },
        )
        return dspy_module.Predict(CanonicalizationSignature)


def _program_inputs(
    task: dict[str, Any],
    output_model: type[BaseModel],
) -> dict[str, str]:
    return {
        "task_json": json.dumps(task, ensure_ascii=False, sort_keys=True),
        "required_json_schema": json.dumps(
            output_model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


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
        raise ValueError(f"DSPy canonicalization returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("DSPy canonicalization must return one JSON object.")
    return payload


def _close_dspy_lm(lm: Any) -> None:
    for target in (lm, getattr(lm, "client", None)):
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                continue
