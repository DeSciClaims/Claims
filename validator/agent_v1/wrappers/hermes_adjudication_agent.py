from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from miner.agent_v1.runtime.usage import usage_from_hermes_stdout


USAGE_PREFIX = "CLAIMS_HERMES_USAGE_JSON="
ALLOWED_DISPOSITIONS = {
    "include_candidate",
    "exclude_candidate",
    "same_unit",
    "separate_valid_units",
    "candidate_a_only",
    "candidate_b_only",
    "both_invalid",
    "insufficient_information",
}
REQUIRED_RESULT_FIELDS = {
    "disposition",
    "material_findings",
    "cited_span_ids",
    "confidence",
    "rationale",
    "insufficient_information",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a schema-validated Hermes adjudication skill task.")
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--skill-file", type=Path, required=True)
    parser.add_argument("--inner-command-json", required=True)
    parser.add_argument("--expected-tracking-ids-json", default="[]")
    parser.add_argument("--timeout", type=float, default=0.0)
    args = parser.parse_args()
    try:
        inner_command = json.loads(args.inner_command_json)
        expected_tracking_ids = json.loads(args.expected_tracking_ids_json)
    except json.JSONDecodeError as exc:
        print(f"Invalid Hermes adjudication wrapper arguments: {exc}", file=sys.stderr)
        return 2
    if not isinstance(inner_command, list) or not all(isinstance(item, str) for item in inner_command):
        print("Hermes inner command must be a JSON array of strings.", file=sys.stderr)
        return 2
    if not isinstance(expected_tracking_ids, list) or not all(isinstance(item, str) for item in expected_tracking_ids):
        print("Expected tracking IDs must be a JSON array of strings.", file=sys.stderr)
        return 2
    return run_agent_task(
        task_file=args.task_file,
        skill_file=args.skill_file,
        inner_command=inner_command,
        expected_tracking_ids=expected_tracking_ids,
        timeout_seconds=max(0.0, args.timeout),
    )


def run_agent_task(
    *,
    task_file: Path,
    skill_file: Path,
    inner_command: list[str],
    expected_tracking_ids: list[str],
    timeout_seconds: float,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    if not task_file.is_file() or not skill_file.is_file():
        print("Hermes adjudication task or skill file is missing.", file=sys.stderr)
        return 2
    work_dir = Path(tempfile.mkdtemp(prefix="claims-silver-hermes-agent-", dir=str(task_file.parent)))
    os.chmod(work_dir, 0o700)
    output_file = work_dir / "adjudication_output.json"
    schema_file = work_dir / "adjudication_output_schema.json"
    schema_file.write_text(
        json.dumps(_output_schema(expected_tracking_ids), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(schema_file, 0o600)
    query = _agent_query(
        task_file=task_file,
        skill_file=skill_file,
        schema_file=schema_file,
        output_file=output_file,
    )
    command = [*inner_command, query]
    try:
        runner = process_runner or _run_inner_agent
        completed = runner(
            command=command,
            cwd=work_dir,
            output_file=output_file,
            expected_tracking_ids=expected_tracking_ids,
            timeout_seconds=timeout_seconds or None,
        )
        payload = _read_valid_payload(output_file, expected_tracking_ids)
        if payload is None:
            payload = _valid_payload_from_text(completed.stdout, expected_tracking_ids)
        _forward_inner_output(completed)
        _emit_usage(completed.stdout, inner_command)
        if payload is None:
            print("Hermes completed without a valid adjudication artifact.", file=sys.stderr)
            return completed.returncode or 1
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException as exc:
        print(f"Hermes adjudication agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _agent_query(*, task_file: Path, skill_file: Path, schema_file: Path, output_file: Path) -> str:
    return "\n".join(
        [
            "Run the anonymous Claims Silver adjudication task using the preloaded claims-silver-adjudicator skill.",
            f"Skill instructions: {skill_file}",
            f"Task: {task_file}",
            f"Output JSON Schema: {schema_file}",
            f"Required output: {output_file}",
            "Read the complete task and schema, resolve every supplied case, and write only strict JSON to the required output file.",
            "Do not change any case_tracking_id. Do not return before the output file has been written and checked against the schema.",
            "After writing the file, print FINAL_JSON followed by the same JSON object.",
        ]
    )


def _run_inner_agent(
    *,
    command: list[str],
    cwd: Path,
    output_file: Path,
    expected_tracking_ids: list[str],
    timeout_seconds: float | None,
) -> subprocess.CompletedProcess[str]:
    stdout_path = cwd / ".hermes_stdout.txt"
    stderr_path = cwd / ".hermes_stderr.txt"
    poll_seconds = max(0.1, float(os.getenv("CLAIMS_SILVER_ADJUDICATION_OUTPUT_POLL_SECONDS", "0.5")))
    stable_seconds = max(0.0, float(os.getenv("CLAIMS_SILVER_ADJUDICATION_OUTPUT_STABLE_SECONDS", "1.0")))
    started = time.monotonic()
    valid_since: float | None = None
    last_state: tuple[int, int] | None = None
    timed_out = False
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        while True:
            returncode = process.poll()
            if returncode is not None:
                break
            now = time.monotonic()
            if timeout_seconds is not None and now - started >= timeout_seconds:
                timed_out = True
                returncode = _terminate_process(process)
                break
            state = _valid_file_state(output_file, expected_tracking_ids)
            if state is None:
                valid_since = None
                last_state = None
            elif state == last_state:
                if valid_since is not None and now - valid_since >= stable_seconds:
                    returncode = _terminate_process(process)
                    break
            else:
                last_state = state
                valid_since = now
            time.sleep(poll_seconds)
    stdout = stdout_path.read_text(encoding="utf-8")
    stderr = stderr_path.read_text(encoding="utf-8")
    valid_output = _read_valid_payload(output_file, expected_tracking_ids) is not None
    if valid_output and returncode not in (0, None):
        returncode = 0
        stderr = "\n".join(part for part in [stderr.rstrip(), "Recovered after valid adjudication output was written."] if part)
    elif timed_out:
        stderr = "\n".join(part for part in [stderr.rstrip(), f"Hermes adjudication timed out after {timeout_seconds}s."] if part)
    return subprocess.CompletedProcess(command, int(returncode or 0), stdout=stdout, stderr=stderr)


def _terminate_process(process: subprocess.Popen[str]) -> int:
    process.terminate()
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=5)


def _valid_file_state(path: Path, expected_tracking_ids: list[str]) -> tuple[int, int] | None:
    payload = _read_valid_payload(path, expected_tracking_ids)
    if payload is None:
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _read_valid_payload(path: Path, expected_tracking_ids: list[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if _is_valid_payload(payload, expected_tracking_ids) else None


def _valid_payload_from_text(text: str, expected_tracking_ids: list[str]) -> dict[str, Any] | None:
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _is_valid_payload(payload, expected_tracking_ids):
            return payload
    return None


def _is_valid_payload(payload: Any, expected_tracking_ids: list[str]) -> bool:
    if not isinstance(payload, dict):
        return False
    if expected_tracking_ids:
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(expected_tracking_ids):
            return False
        returned_ids = []
        for result in results:
            if not isinstance(result, dict) or not _is_valid_result(result, require_tracking_id=True):
                return False
            returned_ids.append(str(result.get("case_tracking_id") or ""))
        return len(set(returned_ids)) == len(returned_ids) and set(returned_ids) == set(expected_tracking_ids)
    return _is_valid_result(payload, require_tracking_id=False)


def _is_valid_result(payload: dict[str, Any], *, require_tracking_id: bool) -> bool:
    required = REQUIRED_RESULT_FIELDS | ({"case_tracking_id"} if require_tracking_id else set())
    if not required.issubset(payload):
        return False
    if payload.get("disposition") not in ALLOWED_DISPOSITIONS:
        return False
    if not isinstance(payload.get("material_findings"), list) or not all(
        isinstance(item, str) for item in payload["material_findings"]
    ):
        return False
    if not isinstance(payload.get("cited_span_ids"), list) or not all(
        isinstance(item, str) for item in payload["cited_span_ids"]
    ):
        return False
    confidence = payload.get("confidence")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        return False
    return isinstance(payload.get("rationale"), str) and isinstance(payload.get("insufficient_information"), bool)


def _output_schema(expected_tracking_ids: list[str]) -> dict[str, Any]:
    result_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(REQUIRED_RESULT_FIELDS | ({"case_tracking_id"} if expected_tracking_ids else set())),
        "properties": {
            "disposition": {"type": "string", "enum": sorted(ALLOWED_DISPOSITIONS)},
            "material_findings": {"type": "array", "items": {"type": "string"}},
            "cited_span_ids": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
            "insufficient_information": {"type": "boolean"},
        },
    }
    if not expected_tracking_ids:
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", **result_schema}
    result_schema["properties"]["case_tracking_id"] = {"type": "string", "enum": expected_tracking_ids}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "minItems": len(expected_tracking_ids),
                "maxItems": len(expected_tracking_ids),
                "items": result_schema,
            }
        },
    }


def _json_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates: list[str] = []
    marker_index = stripped.rfind("FINAL_JSON:")
    if marker_index >= 0:
        marked = _balanced_json_object(stripped[marker_index + len("FINAL_JSON:") :])
        if marked:
            candidates.append(marked)
    candidates.extend(match.group(1) for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL))
    candidates.extend(_balanced_json_objects(stripped))
    return candidates


def _balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    offset = 0
    while offset < len(text):
        candidate = _balanced_json_object(text[offset:])
        if not candidate:
            break
        start = text.find(candidate, offset)
        objects.append(candidate)
        offset = start + len(candidate)
    return objects


def _balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _forward_inner_output(completed: subprocess.CompletedProcess[str]) -> None:
    if completed.stdout.strip():
        print(completed.stdout.rstrip(), file=sys.stderr)
    if completed.stderr.strip():
        print(completed.stderr.rstrip(), file=sys.stderr)


def _emit_usage(stdout: str, inner_command: list[str]) -> None:
    usage = usage_from_hermes_stdout(stdout, command_prefix=_hermes_command_prefix(inner_command))
    if any(usage.get(key) is not None for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")):
        print(f"{USAGE_PREFIX}{json.dumps(usage, separators=(',', ':'))}", file=sys.stderr)


def _hermes_command_prefix(command: list[str]) -> list[str] | None:
    for index, item in enumerate(command):
        if Path(item).name != "hermes":
            continue
        if index > 0 and Path(command[index - 1]).name in {"python", "python3"}:
            return [command[index - 1], item]
        return [item]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
