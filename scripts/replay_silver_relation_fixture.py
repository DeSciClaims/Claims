from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from validator.agent_v1.comparison_models import ComparisonCandidate
from validator.agent_v1.relation_classifier import (
    CLIRelationClassifier,
    DSPyRelationClassifier,
    OpenAICompatibleRelationClassifier,
    classify_candidate_pair_heuristic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay one fixed Silver relation fixture through a selected harness.")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/silver_relation_replay_v0.json"),
    )
    parser.add_argument(
        "--harness",
        choices=("heuristic", "dspy", "openai-compatible", "cli"),
        default="heuristic",
    )
    parser.add_argument("--model", default=os.getenv("CLAIMS_SILVER_RELATION_MODEL", "openai/gpt-4o-mini"))
    parser.add_argument("--cli-command", default=os.getenv("CLAIMS_SILVER_RELATION_CLI_COMMAND", ""))
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    rows = fixture.get("pairs") if isinstance(fixture.get("pairs"), list) else []
    pairs = [
        (ComparisonCandidate.model_validate(row["left"]), ComparisonCandidate.model_validate(row["right"]))
        for row in rows
    ]
    classifier = _classifier(args)
    if args.harness == "heuristic":
        edges = [classify_candidate_pair_heuristic(left, right) for left, right in pairs]
    else:
        edges = classifier.classify_many(pairs)
    output = {
        "fixture": str(args.fixture),
        "harness": args.harness,
        "model": args.model,
        "matches": sum(
            edge.relation == row.get("expected_relation")
            for edge, row in zip(edges, rows, strict=True)
        ),
        "total": len(rows),
        "results": [
            {
                **edge.model_dump(mode="json"),
                "expected_relation": row.get("expected_relation"),
            }
            for edge, row in zip(edges, rows, strict=True)
        ],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0 if output["matches"] == output["total"] else 1


def _classifier(args: argparse.Namespace):
    common = {
        "model": args.model,
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "api_base": os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
        "batch_size": args.batch_size,
        "fallback_to_heuristic": False,
    }
    if args.harness == "dspy":
        model = args.model if args.model.startswith("openrouter/") else f"openrouter/{args.model}"
        return DSPyRelationClassifier(**{**common, "model": model})
    if args.harness == "openai-compatible":
        return OpenAICompatibleRelationClassifier(**common)
    if args.harness == "cli":
        if not args.cli_command:
            raise SystemExit("--cli-command or CLAIMS_SILVER_RELATION_CLI_COMMAND is required for CLI replay")
        return CLIRelationClassifier(command=args.cli_command, **common)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
