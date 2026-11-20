"""Run source-only DSPy extraction and compare against the four worked examples."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import dspy
from dotenv import load_dotenv

from .accounting import ledger_events, summarize_calls
from .compare import compare
from .pipeline import extract_pdf
from .runtime import DspyRuntime

CASES = {
    "Shukla": ("P001", "Diabetes Care Shukla et al.pdf"),
    "Genther": ("P002", "JAMA Genther et al.pdf"),
    "Ohtsuki": ("P003", "Nature Ohtsuki et al.pdf"),
    "Perel": ("P004", "BMJ Perel et al.pdf"),
}


def run_case(*, name: str, pdf_dir: Path, examples_dir: Path, output_dir: Path,
             runtime: DspyRuntime, supplement: Path | None = None,
             prepared_dir: Path | None = None, vision: bool = False, batch_size: int = 4,
             max_attempts: int = 3) -> dict:
    paper_id, filename = CASES[name]
    started = time.monotonic()
    destination = output_dir / name
    report = {"case": name, "model": runtime.model, "runtime": runtime.runtime_name,
              "dspy_version": dspy.__version__, "vision": vision,
              "reasoning_effort": runtime.reasoning_effort, "max_tokens": runtime.max_tokens}
    try:
        # Expected examples enter only compare(), after source-only extraction.
        extract_pdf(pdf_dir / filename, destination, runtime=runtime, paper_id=paper_id,
                    supplements=[supplement] if supplement is not None and name == "Ohtsuki" else [],
                    prepared_dir=prepared_dir / name if prepared_dir else None,
                    vision=vision, batch_size=batch_size, max_attempts=max_attempts,
                    resume=(destination / "run_identity.json").exists())
        report["status"] = "valid_working_extraction"
    except Exception as exc:
        logging.exception("Extraction failed for %s", name)
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    actual = destination / "extraction.json"
    if not actual.exists():
        actual = destination / "working_extraction.json"
    if actual.exists():
        try:
            report["comparison"] = compare(actual, examples_dir / f"{name}_Extraction_v1.1.json")
        except (KeyError, ValueError, OSError) as exc:
            report["comparison_error"] = str(exc)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["usage"] = [event for event in ledger_events(destination) if event["event"] in {"request_complete", "request_error"}]
    report["accounting"] = summarize_calls(destination)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{name}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("%s finished: %s; calls=%s cost=$%.6f", name, report["status"],
                 report["accounting"]["started_requests"], report["accounting"]["recorded_cost_usd"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--examples-dir", type=Path, default=Path(__file__).resolve().parents[2] / "schemas/scientific-result-extraction/v1.1/examples")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--ohtsuki-supplement", type=Path)
    parser.add_argument("--case", action="append", choices=tuple(CASES))
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash-0731")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "high", "max"), default="none")
    parser.add_argument("--retry-reasoning-effort", choices=("none", "low", "high", "max"), default="low")
    parser.add_argument("--request-deadline", type=float, default=600, help="Absolute seconds per API attempt")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--transport-retries", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    if args.request_deadline <= 0:
        parser.error("--request-deadline must be positive")
    if min(args.batch_size, args.max_tokens, args.max_attempts, args.concurrency) < 1 or args.transport_retries < 0:
        parser.error("Budgets and concurrency must be positive; transport retries must be nonnegative")
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    key = os.getenv("CLAIMS_INFERENCE_TOKEN") or os.getenv("OPENROUTER_API_KEY")
    if not key:
        parser.error("Inference credentials are required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    selected = args.case or list(CASES)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    api_base = os.getenv("CLAIMS_INFERENCE_BASE_URL") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    config = {"runtime": "dspy-predict", "runtime_version": "2",
              "prompt_format": "raw-api-messages", "extraction_profile": "staged-contract-v2", "dspy_version": dspy.__version__, "model": args.model,
              "max_tokens": args.max_tokens, "reasoning_effort": args.reasoning_effort, "retry_reasoning_effort": args.retry_reasoning_effort, "temperature": 0.0,
              "batch_size": args.batch_size, "semantic_attempts": args.max_attempts,
              "transport_retries": args.transport_retries, "request_deadline_seconds": args.request_deadline, "concurrency": args.concurrency,
              "vision": args.vision, "cases": selected, "api_base": api_base,
              "pdf_hashes": {name: hashlib.sha256((args.pdf_dir / CASES[name][1]).read_bytes()).hexdigest() for name in selected},
              "supplement_sha256": hashlib.sha256(args.ohtsuki_supplement.read_bytes()).hexdigest() if args.ohtsuki_supplement else None}
    config["pipeline_sources_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(__file__).parent.glob("*.py")}
    config["prepared_sources_sha256"] = {str(path.relative_to(args.prepared_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for name in selected for path in sorted((args.prepared_dir / name).rglob("*"))
        if path.is_file() and path.suffix in {".json", ".md", ".txt", ".csv"}} if args.prepared_dir else {}
    config_path = args.output_dir / "benchmark_config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        parser.error("Benchmark configuration changed; use a new output directory")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def run(name):
        runtime = DspyRuntime(model=args.model, api_key=key, api_base=api_base,
            model_route="openrouter/" + args.model.removeprefix("openrouter/") if os.getenv("CLAIMS_INFERENCE_BASE_URL") else None,
            max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort, retry_reasoning_effort=args.retry_reasoning_effort, request_deadline=args.request_deadline, transport_retries=args.transport_retries)
        return run_case(name=name, pdf_dir=args.pdf_dir, examples_dir=args.examples_dir,
                        output_dir=args.output_dir, runtime=runtime,
                        supplement=args.ohtsuki_supplement, prepared_dir=args.prepared_dir,
                        vision=args.vision, batch_size=args.batch_size, max_attempts=args.max_attempts)

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        reports = list(executor.map(run, selected))
    (args.output_dir / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(report["status"] == "failed" for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
