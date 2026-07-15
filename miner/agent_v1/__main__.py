from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from .config import AgentV1Config
from .runner import AgentV1Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the skill-capable agent_v1 miner pipeline.")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--artifact-json", type=Path)
    parser.add_argument("--text", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime", choices=("dspy-react", "langchain-agent", "agent-cli"), default=None)
    parser.add_argument("--skill-dir", type=Path)
    parser.add_argument("--max-agent-iters", type=int)
    args = parser.parse_args()
    if sum(bool(value) for value in (args.pdf, args.artifact_json, args.text)) != 1:
        raise SystemExit("Provide exactly one of --pdf, --artifact-json, or --text.")

    base_dir = Path(__file__).resolve().parents[2]
    load_dotenv(base_dir / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    config = AgentV1Config.from_env(base_dir)
    if args.runtime:
        config.runtime = args.runtime
    if args.skill_dir:
        config.skill_dir = args.skill_dir
    if args.max_agent_iters:
        config.max_agent_iters = args.max_agent_iters
    runner = AgentV1Runner(config)
    if args.pdf:
        runner.run_from_pdf(args.pdf, output_dir=args.output_dir)
    elif args.artifact_json:
        runner.run_from_artifact_json(args.artifact_json, output_dir=args.output_dir)
    else:
        runner.run_from_text(args.text, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
