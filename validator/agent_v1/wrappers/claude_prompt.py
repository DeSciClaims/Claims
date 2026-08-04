from __future__ import annotations

import os
import shutil

from .prompt_agent import main as prompt_agent_main


def main() -> int:
    if not os.getenv("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND") and not os.getenv("CLAIMS_AGENT_INNER_COMMAND"):
        claude = shutil.which("claude")
        if claude:
            os.environ["CLAIMS_VALIDATOR_AGENT_INNER_COMMAND"] = f"{claude} -p --permission-mode bypassPermissions"
    return prompt_agent_main()


if __name__ == "__main__":
    raise SystemExit(main())
