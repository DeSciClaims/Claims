from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable


USAGE_PREFIX = "CLAIMS_HERMES_USAGE_JSON="


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one tool-free Hermes turn from a task file.")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--skill-file", type=Path, required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    args = parser.parse_args()
    return run_prompt_file(
        prompt_file=args.prompt_file,
        skill_file=args.skill_file,
        model=args.model,
        provider=args.provider,
    )


def run_prompt_file(
    *,
    prompt_file: Path,
    skill_file: Path,
    model: str = "",
    provider: str = "",
    run_agent: Callable[..., tuple[str, dict[str, Any]]] | None = None,
) -> int:
    task = prompt_file.read_text(encoding="utf-8")
    skill = _skill_body(skill_file.read_text(encoding="utf-8"))
    prompt = f"{skill}\n\n# Adjudication Task\n\n{task}"
    execute = run_agent or _load_run_agent()
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    os.environ["HERMES_YOLO_MODE"] = "1"
    os.environ["HERMES_ACCEPT_HOOKS"] = "1"
    devnull = open(os.devnull, "w", encoding="utf-8")
    try:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            response, result = execute(
                prompt,
                model=model or None,
                provider=provider or None,
                toolsets=[],
                use_config_toolsets=False,
            )
    except BaseException as exc:
        print(f"Hermes one-shot adjudication failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        devnull.close()
        logging.disable(previous_logging_disable)
    response = str(response or "")
    if not response.strip():
        print("Hermes one-shot adjudication returned no response.", file=sys.stderr)
        return 1
    print(response)
    print(f"{USAGE_PREFIX}{json.dumps(_usage_payload(result), separators=(',', ':'))}", file=sys.stderr)
    return 0


def _load_run_agent():
    from hermes_cli import oneshot
    from hermes_cli import mcp_startup

    # Silver adjudication is stateless. Avoid serializing concurrent calls on
    # Hermes's shared session SQLite database and skip MCP discovery because
    # this wrapper deliberately exposes no tools.
    oneshot._create_session_db_for_oneshot = lambda: None
    mcp_startup.ensure_mcp_discovery_before_agent_build = lambda **_kwargs: None
    return oneshot._run_agent


def _skill_body(content: str) -> str:
    if not content.startswith("---"):
        return content.strip()
    end = content.find("\n---", 3)
    return content[end + 4 :].strip() if end >= 0 else content.strip()


def _usage_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "actual_cost_usd",
            "estimated_cost_usd",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "api_calls",
            "model",
            "provider",
        )
    }


if __name__ == "__main__":
    raise SystemExit(main())
