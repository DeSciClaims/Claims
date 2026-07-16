from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from .prompt_agent import main as prompt_agent_main


def main() -> int:
    os.environ.setdefault("CLAIMS_AGENT_PROMPT_MODE", "stdin")
    if not os.getenv("CLAIMS_AGENT_INNER_COMMAND"):
        claude = shutil.which("claude")
        if claude:
            os.environ["CLAIMS_AGENT_INNER_COMMAND"] = _default_command(claude)
    return prompt_agent_main()


def _default_command(claude: str) -> str:
    claims_root = Path(__file__).resolve().parents[3]
    command = [
        claude,
        "-p",
        "--permission-mode",
        os.getenv("CLAIMS_CLAUDE_PERMISSION_MODE", "bypassPermissions"),
        "--output-format",
        os.getenv("CLAIMS_CLAUDE_OUTPUT_FORMAT", "text"),
        "--add-dir",
        str(claims_root),
    ]
    model = os.getenv("CLAIMS_CLAUDE_MODEL")
    if model:
        command.extend(["--model", model])
    extra_args = os.getenv("CLAIMS_CLAUDE_EXTRA_ARGS")
    if extra_args:
        command.extend(shlex.split(extra_args))
    return shlex.join(command)


if __name__ == "__main__":
    raise SystemExit(main())
