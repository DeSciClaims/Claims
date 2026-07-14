from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from .config import AraV1Config
from .runner import AraV1Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile a paper directly into an ARA v1 artifact.")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--artifact-json", type=Path)
    parser.add_argument("--text", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if sum(bool(value) for value in (args.pdf, args.artifact_json, args.text)) != 1:
        raise SystemExit("Provide exactly one of --pdf, --artifact-json, or --text.")

    base_dir = Path(__file__).resolve().parents[2]
    load_dotenv(base_dir / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    runner = AraV1Runner(AraV1Config.from_env(base_dir))
    if args.pdf:
        runner.run_from_pdf(args.pdf, output_dir=args.output_dir)
    elif args.artifact_json:
        runner.run_from_artifact_json(args.artifact_json, output_dir=args.output_dir)
    else:
        runner.run_from_text(args.text, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
