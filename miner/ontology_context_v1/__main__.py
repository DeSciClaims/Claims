from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from .config import OntologyContextV1Config
from .runner import OntologyContextV1Miner
from .validator import OntologyContextV1Validator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ontology_context_v1 miner or validator.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mine = subparsers.add_parser("mine", help="Annotate a section_context_v1 extraction output.")
    mine.add_argument("--extraction-output-json", type=Path, required=True)
    mine.add_argument("--output-dir", type=Path)

    validate = subparsers.add_parser("validate", help="Validate an ontology_context_v1 output JSON.")
    validate.add_argument("--ontology-output-json", type=Path, required=True)
    validate.add_argument("--output-dir", type=Path)

    return parser


def main() -> int:
    base_dir = Path(__file__).resolve().parents[2]
    load_dotenv(base_dir / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    args = build_parser().parse_args()
    config = OntologyContextV1Config.from_env(base_dir)
    if args.command == "mine":
        miner = OntologyContextV1Miner(config)
        miner.run_from_section_context_output(args.extraction_output_json, output_dir=args.output_dir)
        return 0
    if args.command == "validate":
        validator = OntologyContextV1Validator(config)
        validator.validate_output_json(args.ontology_output_json, output_dir=args.output_dir)
        return 0
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
