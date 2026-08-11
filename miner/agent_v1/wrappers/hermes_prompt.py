from __future__ import annotations

import os
import shutil
from pathlib import Path

from .prompt_agent import main as prompt_agent_main


def main() -> int:
    # In production miner runs, returning the valid artifact is more important
    # than waiting for Hermes' session footer. Operators can still set this to
    # false when debugging usage export behavior.
    os.environ.setdefault("CLAIMS_AGENT_EXIT_ON_VALID_OUTPUT", "true")
    if not os.getenv("CLAIMS_AGENT_INNER_COMMAND"):
        hermes = _discover_hermes()
        if hermes:
            os.environ["CLAIMS_AGENT_INNER_COMMAND"] = f"{hermes} chat -q"
    return prompt_agent_main()


def _discover_hermes() -> str:
    on_path = shutil.which("hermes")
    if on_path:
        return on_path
    claims_root = Path(__file__).resolve().parents[3]
    sibling = claims_root.parent / "hermes-agent" / "hermes"
    if sibling.exists():
        return str(sibling)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
