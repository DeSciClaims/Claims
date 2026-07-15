from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic external agent wrapper for validator.agent_v1.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--agent-command",
        default=os.getenv("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND", os.getenv("CLAIMS_AGENT_INNER_COMMAND", "")),
        help="External agent command. Defaults to CLAIMS_VALIDATOR_AGENT_INNER_COMMAND.",
    )
    parser.add_argument(
        "--prompt-mode",
        choices=("append", "stdin"),
        default=os.getenv("CLAIMS_VALIDATOR_AGENT_PROMPT_MODE", os.getenv("CLAIMS_AGENT_PROMPT_MODE", "append")),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("CLAIMS_VALIDATOR_AGENT_INNER_TIMEOUT", os.getenv("CLAIMS_AGENT_INNER_TIMEOUT", "0")) or "0"),
    )
    args = parser.parse_args()
    if not args.agent_command.strip():
        print("Missing --agent-command or CLAIMS_VALIDATOR_AGENT_INNER_COMMAND for validator prompt wrapper.", file=sys.stderr)
        return 2

    prompt = _build_prompt(args.run_dir, args.skill_dir, args.request, args.output)
    (args.run_dir / "rigor_agent_prompt.md").write_text(prompt, encoding="utf-8")
    command = shlex.split(args.agent_command)
    stdin = None
    if args.prompt_mode == "append":
        command.append(prompt)
    else:
        stdin = prompt
    completed = subprocess.run(
        command,
        cwd=str(args.run_dir),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=args.timeout or None,
        check=False,
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
        print(f"Inner validator agent did not write {args.output} or print a JSON object.", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _build_prompt(run_dir: Path, skill_dir: Path, request_path: Path, output_path: Path) -> str:
    request = _read_json(request_path)
    skill_md = skill_dir / "SKILL.md"
    contract_path = skill_dir / "references" / "claims-agent-v1-rigor-output-contract.md"
    return "\n\n".join(
        [
            "# Claims validator.agent_v1 rigor task",
            "You are running inside a Claims subnet validator task run directory.",
            "Use the mounted rigor reviewer skill and produce required structured JSON findings.",
            "Do not ask follow-up questions. Do not write markdown fences around the final JSON.",
            "Do not compute the final score. Deterministic validator code computes scoring.",
            "",
            "## Required output",
            f"Write strict JSON to: `{output_path}`",
            "The JSON must validate against `rigor_findings_schema.json`.",
            "After writing the file, also print `FINAL_JSON:` followed by the same strict JSON object so the wrapper can recover it if file writes are sandboxed.",
            "",
            "## Files",
            f"- Run directory: `{run_dir}`",
            f"- Skill directory: `{skill_dir}`",
            f"- Skill instructions: `{skill_md}`",
            f"- Rigor JSON contract: `{contract_path}`",
            f"- Request: `{request_path}`",
            f"- Artifact: `{run_dir / request.get('artifact_path', 'agent_output.json')}`",
            f"- Source payload: `{run_dir / str(request.get('source_payload_path') or 'source_payload.json')}`",
            f"- Structural findings: `{run_dir / request.get('structural_findings_path', 'structural_findings.json')}`",
            f"- Grounding findings: `{run_dir / request.get('grounding_findings_path', 'grounding_findings.json')}`",
            f"- Output schema: `{run_dir / request.get('output_schema_path', 'rigor_findings_schema.json')}`",
            "",
            "## Mandatory steps",
            "1. Read the skill instructions and rigor JSON contract.",
            "2. Read the artifact, source payload, deterministic findings, and output schema.",
            "3. Review all required rigor dimensions.",
            f"4. Write only the findings object to `{output_path}`.",
            "",
            "If writing files is unavailable, print `FINAL_JSON:` followed by only the final JSON object to stdout.",
        ]
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    for candidate in _json_candidates(text.strip()):
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
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL))
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
