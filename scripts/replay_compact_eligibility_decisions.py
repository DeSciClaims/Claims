"""Replay a negative-eligibility task with a compact decision-only schema.

The prompt still contains the full Claims policy, gate definitions, task, and
source spans. The response keeps candidate-level eligibility and the selected
candidate, but removes rationales, gate-by-gate assessments, and citations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


CLAIMS_ROOT = Path(__file__).resolve().parents[1]
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))

from validator.agent_v1.eligibility_decisions import _GATE_INSTRUCTIONS
from validator.agent_v1.file_agent_workflow import _skill_path


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ref: str
    eligible: bool


class CaseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ref: str
    candidate_decisions: list[CandidateDecision]
    selected_candidate_ref: str | None


class CompactEligibilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[CaseDecision]


class SelectionCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ref: str
    selected_candidate_ref: str | None


class SelectionOnlyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[SelectionCase]


class SingleSelectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_candidate_ref: str | None


def request_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer EMPTY", "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(800).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc


def validate_output(output: CompactEligibilityOutput, task: dict[str, Any]) -> None:
    cases_by_ref = {case["case_ref"]: case for case in task["cases"]}
    returned_cases = {assessment.case_ref for assessment in output.assessments}
    if returned_cases != set(cases_by_ref) or len(output.assessments) != len(cases_by_ref):
        raise ValueError("Response must return each requested case exactly once")
    for assessment in output.assessments:
        case = cases_by_ref[assessment.case_ref]
        expected = {candidate["candidate_ref"] for candidate in case["candidates"]}
        returned = {decision.candidate_ref for decision in assessment.candidate_decisions}
        if returned != expected or len(assessment.candidate_decisions) != len(expected):
            raise ValueError(f"Case {assessment.case_ref} must decide every candidate exactly once")
        eligibility = {decision.candidate_ref: decision.eligible for decision in assessment.candidate_decisions}
        selected = assessment.selected_candidate_ref
        if selected is not None and (selected not in expected or not eligibility[selected]):
            raise ValueError(f"Case {assessment.case_ref} selected an ineligible or unknown candidate")
        if sum(eligibility.values()) == 1:
            only_passing = next(candidate_ref for candidate_ref, passed in eligibility.items() if passed)
            if selected != only_passing:
                raise ValueError(f"Case {assessment.case_ref} did not select its sole eligible candidate")
        if not any(eligibility.values()) and selected is not None:
            raise ValueError(f"Case {assessment.case_ref} selected a candidate although none are eligible")


def selection_reference(path: Path) -> dict[str, str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {case["case_ref"]: case["selected_candidate_ref"] for case in data["cases"]}


def validate_selection_only(output: SelectionOnlyOutput, task: dict[str, Any]) -> None:
    cases_by_ref = {case["case_ref"]: case for case in task["cases"]}
    returned_cases = {decision.case_ref for decision in output.decisions}
    if returned_cases != set(cases_by_ref) or len(output.decisions) != len(cases_by_ref):
        raise ValueError("Response must return each requested case exactly once")
    for decision in output.decisions:
        allowed = {candidate["candidate_ref"] for candidate in cases_by_ref[decision.case_ref]["candidates"]}
        if decision.selected_candidate_ref is not None and decision.selected_candidate_ref not in allowed:
            raise ValueError(f"Case {decision.case_ref} selected an unknown candidate")


def validate_single_selection(output: SingleSelectionOutput, task: dict[str, Any]) -> None:
    if len(task["cases"]) != 1:
        raise ValueError("single-selection requires exactly one case per request")
    allowed = {candidate["candidate_ref"] for candidate in task["cases"][0]["candidates"]}
    if output.selected_candidate_ref is not None and output.selected_candidate_ref not in allowed:
        raise ValueError("Response selected an unknown candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--disable-reasoning", action="store_true")
    parser.add_argument("--contract", choices=("candidate-decisions", "selection-only", "single-selection"), default="candidate-decisions")
    parser.add_argument("--response-format", choices=("json_object", "json_schema"), default="json_object")
    args = parser.parse_args()
    if args.batch_size < 1 or args.max_tokens < 1:
        parser.error("batch size and max tokens must be positive")

    raw_task = args.task.read_bytes()
    task = json.loads(raw_task)
    reference = selection_reference(args.reference)
    cases = task["cases"]
    if set(reference) != {case["case_ref"] for case in cases}:
        raise SystemExit("Reference cases do not match task cases")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    policy = _skill_path("claims-silver-eligibility-selection-negative").read_text(encoding="utf-8")
    if args.contract == "candidate-decisions":
        instruction = (
            "Return each requested case and candidate exactly once. Do not write a rationale, "
            "gate breakdown, source-span list, or any fields outside the schema."
        )
    elif args.contract == "selection-only":
        instruction = (
            "Return each requested case exactly once. `selected_candidate_ref` must be a candidate ID "
            "from that case such as `k0_a`, or null when no candidate is eligible. Do not return a "
            "candidate-level eligibility list, rationale, gate breakdown, or source-span list."
        )
    else:
        instruction = (
            "The request contains one case. Return only `selected_candidate_ref`: a candidate ID such as "
            "`k0_a`, or null when no candidate is eligible. Do not return a case ID, candidate-level "
            "eligibility list, rationale, gate breakdown, or source-span list."
        )
    system = (
        "Apply the Claims negative eligibility policy and all gate definitions to every candidate. "
        "Return exactly one JSON object matching the supplied compact schema. "
        "A candidate is eligible only if it passes every policy gate. "
        + instruction + "\n\n"
        + policy
        + "\n\nEligibility gate definitions:\n"
        + json.dumps(_GATE_INSTRUCTIONS, ensure_ascii=False, sort_keys=True)
    )
    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_path": str(args.task),
        "task_sha256": hashlib.sha256(raw_task).hexdigest(),
        "model": args.model,
        "api_base": args.api_base.rstrip("/"),
        "batch_size": args.batch_size,
        "max_tokens": args.max_tokens,
        "compact_contract": args.contract,
        "response_format": args.response_format,
        "results": [],
    }

    for offset in range(0, len(cases), args.batch_size):
        index = offset // args.batch_size + 1
        batch_task = {**task, "cases": cases[offset : offset + args.batch_size]}
        batch_dir = args.output_dir / f"batch_{index:02d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        output_model = (
            CompactEligibilityOutput if args.contract == "candidate-decisions" else
            SelectionOnlyOutput if args.contract == "selection-only" else
            SingleSelectionOutput
        )
        schema = output_model.model_json_schema()
        if args.contract == "single-selection":
            candidate_refs = [candidate["candidate_ref"] for candidate in batch_task["cases"][0]["candidates"]]
            schema["properties"]["selected_candidate_ref"] = {
                "anyOf": [{"enum": candidate_refs}, {"type": "null"}]
            }
        (batch_dir / "request_schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
        body: dict[str, Any] = {
            "model": args.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "task_json:\n" + json.dumps(batch_task, ensure_ascii=False, sort_keys=True) + "\n\nrequired_json_schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True)},
            ],
            "temperature": 0,
            "max_tokens": args.max_tokens,
            "response_format": (
                {"type": "json_schema", "json_schema": {"name": "compact_eligibility", "strict": True, "schema": schema}}
                if args.response_format == "json_schema" else {"type": "json_object"}
            ),
        }
        if args.disable_reasoning:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        result: dict[str, Any] = {"batch_index": index, "case_refs": [case["case_ref"] for case in batch_task["cases"]]}
        print(f"Running {args.model} batch {index}: {', '.join(result['case_refs'])}", flush=True)
        started = time.perf_counter()
        try:
            response = request_json(f"{args.api_base.rstrip('/')}/chat/completions", body)
            result["duration_seconds"] = round(time.perf_counter() - started, 2)
            result["usage"] = response.get("usage") or {}
            choice = (response.get("choices") or [{}])[0]
            result["finish_reason"] = choice.get("finish_reason")
            raw = ((choice.get("message") or {}).get("content") or "")
            (batch_dir / "raw.txt").write_text(raw, encoding="utf-8")
            result["output_chars"] = len(raw)
            try:
                parsed = output_model.model_validate_json(raw)
                result["json_and_schema_valid"] = True
                if args.contract == "candidate-decisions":
                    validate_output(parsed, batch_task)
                    decisions = {assessment.case_ref: assessment.selected_candidate_ref for assessment in parsed.assessments}
                elif args.contract == "selection-only":
                    validate_selection_only(parsed, batch_task)
                    decisions = {decision.case_ref: decision.selected_candidate_ref for decision in parsed.decisions}
                else:
                    validate_single_selection(parsed, batch_task)
                    decisions = {batch_task["cases"][0]["case_ref"]: parsed.selected_candidate_ref}
                result["contract_valid"] = True
                result["selection_matches_reference"] = sum(
                    decisions[case_ref] == reference[case_ref] for case_ref in decisions
                )
                result["selection_total"] = len(decisions)
                (batch_dir / "parsed.json").write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
                result["status"] = "valid"
            except (ValidationError, ValueError) as exc:
                result["status"] = "schema_or_contract_invalid"
                result["validation_error"] = f"{type(exc).__name__}: {str(exc)[:700]}"
        except Exception as exc:
            result["duration_seconds"] = round(time.perf_counter() - started, 2)
            result["status"] = "request_failed"
            result["error"] = f"{type(exc).__name__}: {str(exc)[:700]}"
        summary["results"].append(result)
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: result.get(key) for key in ("batch_index", "status", "finish_reason", "selection_matches_reference", "selection_total", "validation_error", "error")}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
