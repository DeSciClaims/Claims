"""Replay a saved negative-eligibility task in independent case batches.

Each batch retains the original paper, policy, and source spans, but constrains
the output schema to its own cases. Every response is checked without repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

CLAIMS_ROOT = Path(__file__).resolve().parents[1]
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))

from validator.agent_v1.eligibility import (
    EligibilityAdjudicationAgentOutput,
    assessment_passes_hard_gates,
    normalize_eligibility_assessment,
)
from validator.agent_v1.eligibility_decisions import _GATE_INSTRUCTIONS
from validator.agent_v1.eligibility_dspy import _constrained_output_model
from validator.agent_v1.file_agent_workflow import _skill_path, _validate_eligibility_payload_for_task

API_BASE = "https://openrouter.ai/api/v1"


def request_json(url: str, key: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(1200))
            error = payload.get("error") or {}
            metadata = error.get("metadata") or {}
            detail = "; ".join(str(value) for value in (
                error.get("message"), metadata.get("provider_name"),
                metadata.get("limit_source"), metadata.get("raw"),
            ) if value)
        except (ValueError, AttributeError, TypeError):
            detail = "provider request failed"
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail[:350]}") from exc


def bf16_endpoints(model: str, key: str, max_tokens: int, response_format: str) -> list[dict[str, Any]]:
    data = request_json(f"{API_BASE}/models/{model}/endpoints", key).get("data") or {}
    return [
        {"tag": row.get("tag"), "provider_name": row.get("provider_name"),
         "quantization": row.get("quantization"),
         "max_completion_tokens": row.get("max_completion_tokens")}
        for row in data.get("endpoints") or []
        if row.get("quantization") == "bf16"
        and ("structured_outputs" if response_format == "json_schema" else "response_format")
        in (row.get("supported_parameters") or [])
        and int(row.get("max_completion_tokens") or 0) >= max_tokens
    ]


def validate_selection_consistency(output: EligibilityAdjudicationAgentOutput) -> None:
    """Require the case choice to agree with its own candidate gate assessments."""
    for case in output.assessments:
        passing = [
            candidate.candidate_ref
            for candidate in case.candidate_assessments
            if assessment_passes_hard_gates(normalize_eligibility_assessment(candidate))
        ]
        selected = case.selected_candidate_ref
        if selected is not None and selected not in passing:
            raise ValueError(
                f"Eligibility case {case.case_ref} selected {selected} despite failed candidate gates."
            )
        if len(passing) == 1 and selected != passing[0]:
            raise ValueError(
                f"Eligibility case {case.case_ref} left its sole passing candidate {passing[0]} unselected."
            )


def run_model(
    model: str, task: dict[str, Any], schema: dict[str, Any], key: str,
    max_tokens: int, output_dir: Path, response_format: str,
    disable_reasoning: bool, provider_only: str | None,
    api_base: str, include_gate_definitions: bool,
) -> dict[str, Any]:
    local_server = api_base != API_BASE
    result: dict[str, Any] = {
        "model": model,
        "bf16_schema_endpoints": [] if local_server else bf16_endpoints(model, key, max_tokens, response_format),
        "api_base": api_base,
        "gate_definitions_included": include_gate_definitions,
        "case_count": len(task["cases"]),
        "response_format": response_format,
        "reasoning_enabled": not disable_reasoning,
        "provider_only": provider_only,
        "first_attempt_valid": False,
        "claims_valid": False,
        "decision_consistent": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not local_server and not result["bf16_schema_endpoints"]:
        result["status"] = "no_eligible_bf16_endpoint"
        return result

    system = (
        "Follow the Claims negative eligibility judge policy below. Return exactly one JSON object "
        "matching the supplied schema. Evaluate every case and candidate independently; never invent "
        "source span IDs.\n\n"
        + _skill_path("claims-silver-eligibility-selection-negative").read_text(encoding="utf-8")
    )
    if include_gate_definitions:
        system += "\n\nEligibility gate definitions:\n" + json.dumps(
            _GATE_INSTRUCTIONS, ensure_ascii=False, sort_keys=True
        )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "task_json:\n" + json.dumps(task, ensure_ascii=False, sort_keys=True)
             + "\n\nrequired_json_schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True)},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": (
            {"type": "json_schema", "json_schema": {"name": "claims_negative_eligibility", "strict": True, "schema": schema}}
            if response_format == "json_schema" else {"type": "json_object"}
        ),
    }
    if not local_server:
        body["provider"] = {"quantizations": ["bf16"], "require_parameters": True}
    if disable_reasoning:
        if local_server:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            body["reasoning"] = {"enabled": False}
    if provider_only and not local_server:
        body["provider"]["only"] = [provider_only]
    start = time.perf_counter()
    try:
        response = request_json(f"{api_base}/chat/completions", key, body)
        result["duration_seconds"] = round(time.perf_counter() - start, 2)
        result["generation_id"] = response.get("id")
        result["usage"] = response.get("usage") or {}
        choice = (response.get("choices") or [{}])[0]
        result["finish_reason"] = choice.get("finish_reason")
        raw = ((choice.get("message") or {}).get("content") or "")
        result["output_chars"] = len(raw)
        result["output_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        (output_dir / f"{model.replace('/', '__')}.raw.txt").write_text(raw, encoding="utf-8")
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("Response is not a JSON object")
            result["json_valid"] = True
            _constrained_output_model(EligibilityAdjudicationAgentOutput, task).model_validate(obj)
            parsed = EligibilityAdjudicationAgentOutput.model_validate(obj)
            result["pydantic_valid"] = True
            _validate_eligibility_payload_for_task(parsed, task)
            result["claims_valid"] = True
            validate_selection_consistency(parsed)
            result["decision_consistent"] = True
            result["first_attempt_valid"] = True
            result["status"] = "valid"
            result["returned_case_count"] = len(parsed.assessments)
            result["returned_candidate_count"] = sum(len(case.candidate_assessments) for case in parsed.assessments)
            (output_dir / f"{model.replace('/', '__')}.json").write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError) as exc:
            result["status"] = "schema_or_policy_invalid"
            result["validation_error"] = f"{type(exc).__name__}: {str(exc)[:600]}"
    except Exception as exc:
        result["duration_seconds"] = round(time.perf_counter() - start, 2)
        result["status"] = "request_failed"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:700]}"

    if result.get("generation_id") and not local_server:
        try:
            data = request_json(
                f"{API_BASE}/generation?id={urllib.parse.quote(str(result['generation_id']))}", key
            ).get("data") or {}
            result["provider_name"] = data.get("provider_name")
            result["total_cost"] = data.get("total_cost")
        except Exception as exc:
            result["generation_lookup_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--models", required=True, help="Comma-separated OpenRouter model IDs")
    parser.add_argument("--case-refs", help="Comma-separated case refs to replay; default: all")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--batch-indices", help="Comma-separated 1-based batch numbers; default: all")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--response-format", choices=("json_object", "json_schema"), default="json_object")
    parser.add_argument("--disable-reasoning", action="store_true")
    parser.add_argument("--provider-only")
    parser.add_argument("--api-base", default=API_BASE, help="OpenAI-compatible API base; local vLLM needs no OpenRouter key")
    parser.add_argument("--include-gate-definitions", action="store_true")
    parser.add_argument(
        "--span-scope", choices=("all", "linked"), default="all",
        help="Keep all historical source spans, or only spans linked by the selected candidates",
    )
    args = parser.parse_args()
    if args.batch_size < 1 or args.max_tokens < 1:
        parser.error("batch size and max tokens must be positive")
    selected_batches = None
    if args.batch_indices:
        try:
            selected_batches = {int(value) for value in args.batch_indices.split(",")}
        except ValueError:
            parser.error("batch indices must be comma-separated integers")
        if not selected_batches or min(selected_batches) < 1:
            parser.error("batch indices must be positive")

    load_dotenv(CLAIMS_ROOT / ".env", override=False)
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    api_base = args.api_base.rstrip("/")
    if api_base != API_BASE and not api_base.startswith(("http://127.0.0.1:", "http://localhost:")):
        parser.error("local API base must use a loopback HTTP address")
    if not key and api_base == API_BASE:
        raise SystemExit("OPENROUTER_API_KEY is required")
    if api_base != API_BASE:
        key = "EMPTY"
    raw_task = args.task.read_bytes()
    task = json.loads(raw_task)
    cases = task.get("cases")
    if task.get("judge_role") != "negative" or not isinstance(cases, list) or not cases:
        raise SystemExit("Expected a negative-judge task with cases")
    if args.case_refs:
        requested_refs = {value.strip() for value in args.case_refs.split(",") if value.strip()}
        known_refs = {case.get("case_ref") for case in cases}
        if not requested_refs or not requested_refs <= known_refs:
            parser.error("case refs must name cases in the source task")
        cases = [case for case in cases if case.get("case_ref") in requested_refs]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_path": str(args.task),
        "task_sha256": hashlib.sha256(raw_task).hexdigest(),
        "total_case_count": len(cases),
        "selected_case_refs": [case["case_ref"] for case in cases],
        "batch_size": args.batch_size,
        "max_tokens": args.max_tokens,
        "response_format": args.response_format,
        "reasoning_enabled": not args.disable_reasoning,
        "provider_only": args.provider_only,
        "api_base": api_base,
        "gate_definitions_included": args.include_gate_definitions,
        "span_scope": args.span_scope,
        "results": [],
    }
    for model in [value.strip() for value in args.models.split(",") if value.strip()]:
        for offset in range(0, len(cases), args.batch_size):
            index = offset // args.batch_size + 1
            if selected_batches is not None and index not in selected_batches:
                continue
            batch_task = {**task, "cases": cases[offset:offset + args.batch_size]}
            if args.span_scope == "linked":
                linked_ids = {
                    span_id
                    for case in batch_task["cases"]
                    for candidate in case["candidates"]
                    for span_id in candidate.get("source_span_ids", [])
                }
                batch_task["source_spans"] = {
                    span_id: value
                    for span_id, value in task["source_spans"].items()
                    if span_id in linked_ids
                }
                if set(batch_task["source_spans"]) != linked_ids:
                    raise SystemExit(f"Missing candidate-linked source spans for batch {index}")
            case_refs = [case["case_ref"] for case in batch_task["cases"]]
            batch_dir = args.output_dir / model.replace("/", "__") / f"batch_{index:02d}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            schema = _constrained_output_model(EligibilityAdjudicationAgentOutput, batch_task).model_json_schema()
            (batch_dir / "request_schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
            print(f"Running {model} batch {index}: {', '.join(case_refs)}", flush=True)
            result = run_model(
                model, batch_task, schema, key, args.max_tokens, batch_dir,
                args.response_format, args.disable_reasoning, args.provider_only,
                api_base, args.include_gate_definitions,
            )
            result["batch_index"] = index
            result["case_refs"] = case_refs
            result["input_span_count"] = len(batch_task["source_spans"])
            result["schema_sha256"] = hashlib.sha256(
                json.dumps(schema, sort_keys=True).encode()
            ).hexdigest()
            summary["results"].append(result)
            (args.output_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(json.dumps({
                "model": model, "batch": index, "status": result.get("status"),
                "finish_reason": result.get("finish_reason"),
                "duration_seconds": result.get("duration_seconds"),
                "validation_error": result.get("validation_error"),
                "error": result.get("error"),
            }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
