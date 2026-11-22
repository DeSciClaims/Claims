"""Parse or extract PDFs into scientific result schema v1.1."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--supplement", action="append", default=[], type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--paper-id")
    parser.add_argument("--extraction-id", default="E001")
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "high", "max"), default="none")
    parser.add_argument("--retry-reasoning-effort", choices=("none", "low", "high", "max"), default="low")
    parser.add_argument("--request-deadline", type=float, default=600, help="Absolute seconds per API attempt")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--vision", action="store_true", help="Include page images; requires a vision-capable model")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.request_deadline <= 0:
        parser.error("--request-deadline must be positive")
    if not args.pdf.is_file() or any(not path.is_file() for path in args.supplement):
        parser.error("An input PDF does not exist")
    if args.output_dir.exists() and any(args.output_dir.iterdir()) and not args.resume:
        parser.error("Use an empty output directory")
    if args.prepare_only:
        from .docling_source import parse_pdf
        parse_pdf(args.pdf, args.output_dir)
        return
    if not args.paper_id:
        parser.error("--paper-id is required for extraction")
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    api_key = os.getenv("CLAIMS_INFERENCE_TOKEN") or os.getenv("OPENROUTER_API_KEY")
    model = args.model or os.getenv("CLAIMS_SCIENTIFIC_RESULT_MODEL", "deepseek/deepseek-v4-flash-0731")
    if not model or not api_key:
        parser.error("Set --model or CLAIMS_SCIENTIFIC_RESULT_MODEL, and scoped gateway or OpenRouter credentials")
    from .pipeline import extract_pdf
    from .runtime import DspyRuntime
    api_base = args.api_base or os.getenv("CLAIMS_INFERENCE_BASE_URL") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    runtime = DspyRuntime(model=model, api_key=api_key, api_base=api_base,
                          model_route="openrouter/" + model.removeprefix("openrouter/")
                          if os.getenv("CLAIMS_INFERENCE_BASE_URL") == api_base else None,
                          max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort, retry_reasoning_effort=args.retry_reasoning_effort, request_deadline=args.request_deadline)
    extract_pdf(args.pdf, args.output_dir, runtime=runtime, paper_id=args.paper_id,
                extraction_id=args.extraction_id, supplements=args.supplement,
                batch_size=args.batch_size, max_attempts=args.max_attempts,
                prepared_dir=args.prepared_dir, vision=args.vision, resume=args.resume)


if __name__ == "__main__":
    main()
