from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from .config import SectionContextV1Config
from .runner import SectionContextV1Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the section-context-first miner pipeline on one paper.")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--tei-xml", type=Path)
    parser.add_argument("--artifact-json", type=Path)
    parser.add_argument(
        "--pdf-extraction-method",
        choices=("grobid", "pypdf"),
        default="grobid",
        help="How to parse a PDF input before mining.",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if sum(bool(value) for value in (args.pdf, args.tei_xml, args.artifact_json)) != 1:
        raise SystemExit("Provide exactly one of --pdf, --tei-xml, or --artifact-json.")
    base_dir = Path(__file__).resolve().parents[2]
    load_dotenv(base_dir / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    config = SectionContextV1Config.from_env(base_dir)
    runner = SectionContextV1Runner(config)
    if args.pdf:
        runner.run_from_pdf(
            args.pdf,
            output_dir=args.output_dir,
            extraction_method=args.pdf_extraction_method,
        )
    elif args.tei_xml:
        runner.run_from_tei_xml(args.tei_xml, output_dir=args.output_dir)
    else:
        runner.run_from_artifact_json(args.artifact_json, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
