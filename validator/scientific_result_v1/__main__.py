"""Validate a JSON/CSV scientific package against controller-owned sources."""

import argparse
import os
from pathlib import Path

from .engine import validate
from .sources import load_bundle, strict_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--paper-id")
    parser.add_argument("--pdf-sha256")
    parser.add_argument(
        "--scope",
        choices=["miner_diagnostic", "reference_preflight", "trusted"],
        default="miner_diagnostic",
    )
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash-0731")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()
    runtime = None
    if args.review:
        if not args.source_bundle:
            parser.error("--review requires --source-bundle")
        from .source_review import ReviewRuntime

        key = os.getenv("CLAIMS_INFERENCE_TOKEN") or os.getenv("OPENROUTER_API_KEY")
        if not key:
            parser.error("A model API key is required for source review")
        base = os.getenv("CLAIMS_INFERENCE_BASE_URL") or os.getenv(
            "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
        )
        runtime = ReviewRuntime(
            model=args.model,
            api_key=key,
            api_base=base,
            model_route="openrouter/" + args.model.removeprefix("openrouter/")
            if os.getenv("CLAIMS_INFERENCE_BASE_URL")
            else None,
            reasoning_effort="none",
            max_tokens=16384,
            request_deadline=180,
            transport_retries=0,
        )
    parse_error = None
    try:
        payload = strict_json(args.extraction.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        payload = None
        parse_error = str(exc)
    report = validate(
        payload,
        args.extraction.parent,
        sources=load_bundle(args.source_bundle) if args.source_bundle else None,
        runtime=runtime,
        expected_paper_id=args.paper_id,
        expected_pdf_sha256=args.pdf_sha256,
        scope=args.scope,
        report_dir=args.report_dir,
        batch_size=args.batch_size,
        max_attempts=args.max_attempts,
    )
    if parse_error:
        import json

        report["structural_findings"].insert(
            0, {"code": "schema", "path": "$", "message": parse_error}
        )
        (args.report_dir / "validation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    print(
        f"{report['decision']}: structural={report['structurally_valid']}, scientific={report['scientific_review_status']}, ready={report['submission_ready']}"
    )
    raise SystemExit(0 if report["submission_ready"] else 1)


if __name__ == "__main__":
    main()
