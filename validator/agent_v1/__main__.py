from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import AgentV1ValidatorConfig
from .runner import AgentV1ValidatorRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Claims agent_v1 validator.")
    parser.add_argument("--agent-json", type=Path, help="Path to agent_output.json.")
    parser.add_argument("--source-payload", type=Path, help="Path to source_payload.json from the miner run.")
    parser.add_argument("--output-dir", type=Path, help="Directory for validator outputs.")
    parser.add_argument("--runtime", choices=("dspy-react", "langchain-agent", "agent-cli"), help="Rigor agent runtime.")
    parser.add_argument("--skill-dir", type=Path, help="Rigor reviewer skill directory.")
    parser.add_argument("--max-agent-iters", type=int, help="Native rigor agent loop iteration budget.")
    parser.add_argument("--threshold", type=float, default=0.7, help="Passing score threshold.")
    parser.add_argument("--skip-rigor-agent", action="store_true", help="Run deterministic checks only.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    artifact_path = args.agent_json
    if artifact_path is None:
        parser.error("--agent-json is required")

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s %(message)s")
    config = AgentV1ValidatorConfig.from_env()
    if args.runtime:
        config.runtime = args.runtime
    if args.skill_dir:
        config.skill_dir = args.skill_dir
    if args.max_agent_iters:
        config.max_agent_iters = args.max_agent_iters
    if args.skip_rigor_agent:
        config.skip_rigor_agent = True

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=args.source_payload,
        output_dir=args.output_dir,
        threshold=args.threshold,
    )
    print(json.dumps({"passed": report.passed, "score": report.score, "summary": report.summary}, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
