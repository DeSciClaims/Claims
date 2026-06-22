from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from .config import JudgeV1Config
from .runner import JudgeV1Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge one section_context_v1 extraction output.")
    parser.add_argument("--extraction-output-json", type=Path, required=True)
    parser.add_argument("--mode", choices=("gold", "intrinsic"), default="intrinsic")
    parser.add_argument("--gold-reviewed-file", "--reviewed-file", dest="gold_reviewed_file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--judge-version", choices=("none", "v1"), default="v1")
    parser.add_argument("--xlsx", action="store_true")
    args = parser.parse_args()
    base_dir = Path(__file__).resolve().parents[2]
    load_dotenv(base_dir / ".env")
    config = JudgeV1Config.from_env(base_dir)
    runner = JudgeV1Runner(config)
    runner.judge_extraction_output_json(
        extraction_output_json_path=args.extraction_output_json,
        mode=args.mode,
        gold_reviewed_file=args.gold_reviewed_file,
        output_dir=args.output_dir,
        judge_version=args.judge_version,
        xlsx=args.xlsx,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
