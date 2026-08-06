from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from miner.agent_v1.artifact import materialize_agent_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic external agent wrapper for agent_v1.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--agent-command",
        default=os.getenv("CLAIMS_AGENT_INNER_COMMAND", ""),
        help="External agent command. Defaults to CLAIMS_AGENT_INNER_COMMAND.",
    )
    parser.add_argument(
        "--prompt-mode",
        choices=("append", "stdin"),
        default=os.getenv("CLAIMS_AGENT_PROMPT_MODE", "append"),
        help="Pass the generated prompt as the final argv item or via stdin.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("CLAIMS_AGENT_INNER_TIMEOUT", "0") or "0"),
        help="Optional timeout for the inner agent command.",
    )
    args = parser.parse_args()
    if not args.agent_command.strip():
        print(
            "Missing --agent-command or CLAIMS_AGENT_INNER_COMMAND for prompt_agent wrapper.",
            file=sys.stderr,
        )
        return 2

    prompt = _build_prompt(args.run_dir, args.skill_dir, args.request, args.output)
    prompt_path = args.run_dir / "agent_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    command = shlex.split(args.agent_command)
    if args.prompt_mode == "append":
        command.append(prompt)
        stdin = None
    else:
        stdin = prompt

    completed = _run_inner_agent(
        command=command,
        cwd=args.run_dir,
        stdin=stdin,
        output=args.output,
        timeout=args.timeout or None,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        return completed.returncode

    if args.output.exists():
        return 0
    extracted = _extract_json_object(completed.stdout)
    if extracted is None:
        print(
            f"Inner agent completed but did not write {args.output} or print a JSON object.",
            file=sys.stderr,
        )
        return 1
    args.output.write_text(json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _run_inner_agent(
    *,
    command: list[str],
    cwd: Path,
    stdin: str | None,
    output: Path,
    timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    if _env_flag("CLAIMS_AGENT_EXIT_ON_VALID_OUTPUT", default=True):
        return _run_inner_agent_with_output_watch(command=command, cwd=cwd, stdin=stdin, output=output, timeout=timeout)
    return subprocess.run(
        command,
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_inner_agent_with_output_watch(
    *,
    command: list[str],
    cwd: Path,
    stdin: str | None,
    output: Path,
    timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    stdout_path = cwd / ".agent_inner_stdout.txt"
    stderr_path = cwd / ".agent_inner_stderr.txt"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    poll_seconds = float(os.getenv("CLAIMS_AGENT_OUTPUT_POLL_SECONDS", "1"))
    stable_seconds = float(os.getenv("CLAIMS_AGENT_OUTPUT_STABLE_SECONDS", "2"))
    started = time.monotonic()
    valid_since: float | None = None
    last_state: tuple[int, int] | None = None

    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin)
            process.stdin.close()
        while True:
            returncode = process.poll()
            if returncode is not None:
                break
            now = time.monotonic()
            if timeout is not None and now - started >= timeout:
                returncode = _terminate_process(process)
                break
            state = _valid_json_file_state(output)
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
            time.sleep(max(0.1, poll_seconds))

    stdout = stdout_path.read_text(encoding="utf-8")
    stderr = stderr_path.read_text(encoding="utf-8")
    if returncode not in (0, None) and _valid_json_file_state(output) is not None:
        stderr = "\n".join(part for part in [stderr.rstrip(), "Recovered after valid output was written."] if part)
        returncode = 0
    elif timeout is not None and time.monotonic() - started >= timeout and returncode not in (0, None):
        stderr = "\n".join(part for part in [stderr.rstrip(), f"Inner agent timed out after {timeout}s."] if part)
    return subprocess.CompletedProcess(command, int(returncode or 0), stdout=stdout, stderr=stderr)


def _build_prompt(run_dir: Path, skill_dir: Path, request_path: Path, output_path: Path) -> str:
    request = _read_json(request_path)
    source_path = run_dir / str(request.get("source_payload_path", "source_payload.json"))
    schema_path = run_dir / str(request.get("output_schema_path", "agent_schema.json"))
    feedback_path = run_dir / str(request.get("validation_feedback_path", "validation_feedback.json"))
    contract_path = skill_dir / "references" / "claims-agent-v1-json-output-contract.md"
    skill_md = skill_dir / "SKILL.md"
    return "\n\n".join(
        [
            "# Claims agent_v1 ARA compile task",
            "You are running inside a Claims subnet miner task run directory.",
            "Use the mounted ARA compiler skill and produce the required structured JSON artifact.",
            "Do not ask follow-up questions. Do not write markdown fences around the final JSON.",
            "Do not print a plan, a list of tool calls, or pseudo function calls. If tools are available, actually call them.",
            "The final artifact is an agent JSON object, not an attestation, credential, NGDL claim, or single-claim envelope.",
            "",
            "## Required output",
            f"Write strict JSON to: `{output_path}`",
            "The JSON must validate against the generated schema.",
            "The top-level JSON object must contain: `ara_version`, `paper`, `logic`, `evidence`, `trace`, `src`, and `metadata`.",
            "After writing the file, also print `FINAL_JSON:` followed by the same strict JSON object so the wrapper can recover it if file writes are sandboxed.",
            "",
            "## Files",
            f"- Run directory: `{run_dir}`",
            f"- Skill directory: `{skill_dir}`",
            f"- Skill instructions: `{skill_md}`",
            f"- Claims JSON contract: `{contract_path}`",
            f"- Request: `{request_path}`",
            f"- Source payload: `{source_path}`",
            f"- Output JSON Schema: `{schema_path}`",
            f"- Validation feedback: `{feedback_path}`",
            "",
            "## Mandatory steps",
            "1. Read the skill instructions and Claims JSON contract.",
            "2. Read `source_payload.json` and `agent_schema.json`.",
            "3. Compile a source-bounded ARA artifact.",
            "4. Validate internally against the schema as best you can.",
            f"5. Write the final JSON object to `{output_path}`.",
            "",
            "If writing files is unavailable, print `FINAL_JSON:` followed by only the final JSON object to stdout.",
        ]
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _terminate_process(process: subprocess.Popen[str]) -> int:
    process.terminate()
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=5)


def _valid_json_file_state(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        payload = json.loads(path.read_text(encoding="utf-8"))
        materialize_agent_artifact(payload)
    except Exception:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    for candidate in _json_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    marker_index = text.rfind("FINAL_JSON:")
    if marker_index >= 0:
        marker_candidate = _balanced_json_object(text[marker_index + len("FINAL_JSON:") :])
        if marker_candidate:
            candidates.insert(0, marker_candidate)
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


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


if __name__ == "__main__":
    raise SystemExit(main())
