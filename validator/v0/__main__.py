from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from validator.judge_v1.config import JudgeV1Config

from .runner import JudgeV2Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Create v0 claim-evidence audit records for miner.v0 output.")
    parser.add_argument("--extraction-output-json", type=Path, required=True)
    parser.add_argument("--mode", choices=("intrinsic_audit", "gold_comparison", "intrinsic", "gold"), default="intrinsic_audit")
    parser.add_argument("--gold-reviewed-file", "--reviewed-file", dest="gold_reviewed_file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--extraction-run-id")
    parser.add_argument("--audit-version", default="v2")
    parser.add_argument("--audit-method", choices=("deterministic", "llm"), default="deterministic")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parents[2]
    load_dotenv(base_dir / ".env")
    runner = JudgeV2Runner(JudgeV1Config.from_env(base_dir))
    runner.judge_extraction_output_json(
        extraction_output_json_path=args.extraction_output_json,
        mode=args.mode,
        gold_reviewed_file=args.gold_reviewed_file,
        output_dir=args.output_dir,
        extraction_run_id=args.extraction_run_id,
        audit_version=args.audit_version,
        audit_method=args.audit_method,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
