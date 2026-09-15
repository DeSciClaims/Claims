from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


CLAIMS_ROOT = Path(__file__).resolve().parents[1]
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))

from validator.agent_v1.eligibility import EligibilityAdjudicationAgentOutput
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    FileAgentWorkflowSession,
    _skill_path,
    _validate_eligibility_payload_for_task,
)
from validator.agent_v1.model_usage import ModelUsageCollector


PRIMARY_NEGATIVE_STAGE = re.compile(r"eligibility_adjudication_negative(?:_b\d{3})?")
DEFAULT_RUN_IDS = (
    "run_20260913_223737_ab2d45",
    "run_20260914_040628_1ca940",
    "run_20260914_093535_e0fd25",
)


@dataclass(frozen=True)
class Sample:
    sample_id: str
    run_id: str
    paper_id: str
    source_path: Path
    task: dict[str, Any]
    input_bytes: int
    case_count: int
    singleton_count: int
    pair_count: int


def main() -> int:
    args = _parse_args()
    load_dotenv(CLAIMS_ROOT / ".env", override=False)
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    run_ids = _run_ids(args.run_ids)
    candidates = discover_samples(
        args.workspace_root.expanduser().resolve(),
        run_ids=run_ids,
        cases_per_sample=args.cases_per_sample,
    )
    samples = select_spread_samples(
        candidates,
        sample_count=args.sample_count,
        distinct_papers=not args.allow_repeated_papers,
    )
    if len(samples) < args.sample_count:
        raise SystemExit(
            f"Found only {len(samples)} suitable samples; requested {args.sample_count}."
        )

    manifest = {
        "schema": "claims_adjudication_harness_benchmark_v1",
        "created_at": _now(),
        "workspace_root": str(args.workspace_root),
        "model": args.model,
        "provider": args.provider,
        "harnesses": list(args.harnesses),
        "sample_count": len(samples),
        "cases_per_sample": args.cases_per_sample,
        "samples": [sample_metadata(sample) for sample in samples],
        "results": [],
    }
    _write_json(output_root / "benchmark_manifest.json", manifest)
    for sample in samples:
        sample_dir = output_root / "samples" / sample.sample_id
        sample_dir.mkdir(parents=True, exist_ok=True)
        _write_json(sample_dir / "task.json", sample.task)

    if args.dry_run:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0

    _require_credentials(args)
    for sample_index, sample in enumerate(samples):
        harnesses = list(args.harnesses)
        if sample_index % 2:
            harnesses.reverse()
        for harness in harnesses:
            print(
                f"Running {sample.sample_id} with {harness}: "
                f"{sample.case_count} cases, {sample.input_bytes} input bytes",
                flush=True,
            )
            result = run_sample(
                sample,
                harness=harness,
                output_root=output_root,
                model=args.model,
                provider=args.provider,
                api_base=args.api_base,
                api_key_env=args.api_key_env,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout,
                max_turns=args.max_turns,
            )
            manifest["results"].append(result)
            _write_json(output_root / "benchmark_results.json", manifest)
            write_csv(output_root / "benchmark_results.csv", manifest["results"])

    manifest["comparisons"] = compare_harnesses(manifest["results"])
    manifest["aggregate"] = aggregate_results(manifest["results"], manifest["comparisons"])
    manifest["completed_at"] = _now()
    _write_json(output_root / "benchmark_results.json", manifest)
    write_csv(output_root / "benchmark_results.csv", manifest["results"])
    print(
        json.dumps(
            {
                "comparisons": manifest["comparisons"],
                "aggregate": manifest["aggregate"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )
    print(f"Results: {output_root / 'benchmark_results.json'}", flush=True)
    return 0


def _parse_args() -> argparse.Namespace:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(
        description=(
            "Compare Hermes and DSPy eligibility-selection adjudication using "
            "persisted production task inputs without writing to the backend."
        )
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("/tmp/claims-silver-workspaces"),
    )
    parser.add_argument("--run-ids", default=",".join(DEFAULT_RUN_IDS))
    parser.add_argument("--sample-count", type=int, default=3)
    parser.add_argument("--cases-per-sample", type=int, default=12)
    parser.add_argument(
        "--allow-repeated-papers",
        action="store_true",
        help="Allow distinct case batches from the same run and paper.",
    )
    parser.add_argument(
        "--harnesses",
        type=_harnesses,
        default=("hermes-cli", "dspy"),
    )
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--api-base", default="https://openrouter.ai/api/v1")
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("/data/benchmarks") / f"adjudication-{stamp}",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _harnesses(value: str) -> tuple[str, ...]:
    harnesses = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = sorted(set(harnesses).difference({"hermes-cli", "dspy"}))
    if unknown:
        raise argparse.ArgumentTypeError(f"Unsupported harnesses: {', '.join(unknown)}")
    if not harnesses:
        raise argparse.ArgumentTypeError("At least one harness is required.")
    return harnesses


def _run_ids(value: str) -> tuple[str, ...]:
    run_ids = tuple(item.strip() for item in value.split(",") if item.strip())
    if not run_ids:
        raise SystemExit("At least one run ID is required.")
    return run_ids


def discover_samples(
    workspace_root: Path,
    *,
    run_ids: tuple[str, ...],
    cases_per_sample: int,
) -> list[Sample]:
    samples: list[Sample] = []
    for run_id in run_ids:
        pattern = f"silver_{run_id}_*/*/executions/*/task.json"
        for task_path in sorted(workspace_root.glob(pattern)):
            if not PRIMARY_NEGATIVE_STAGE.fullmatch(task_path.parent.name):
                continue
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cases = task.get("cases")
            if not isinstance(cases, list) or len(cases) != cases_per_sample:
                continue
            case_sizes = [
                len(case.get("candidates") or [])
                for case in cases
                if isinstance(case, dict)
            ]
            if len(case_sizes) != cases_per_sample or any(size not in {1, 2} for size in case_sizes):
                continue
            paper = task.get("paper") if isinstance(task.get("paper"), dict) else {}
            paper_id = str(paper.get("paper_id") or task_path.parents[2].name)
            raw = json.dumps(task, ensure_ascii=False, sort_keys=True).encode("utf-8")
            samples.append(
                Sample(
                    sample_id="",
                    run_id=run_id,
                    paper_id=paper_id,
                    source_path=task_path,
                    task=_base_task(task),
                    input_bytes=len(raw),
                    case_count=len(cases),
                    singleton_count=sum(size == 1 for size in case_sizes),
                    pair_count=sum(size == 2 for size in case_sizes),
                )
            )
    return samples


def select_spread_samples(
    candidates: list[Sample],
    *,
    sample_count: int,
    distinct_papers: bool = True,
) -> list[Sample]:
    if sample_count <= 0:
        return []
    ordered = sorted(candidates, key=lambda item: (item.input_bytes, item.run_id, item.paper_id))
    selected: list[Sample] = []
    used_papers: set[tuple[str, str]] = set()
    for index in range(sample_count):
        target = round(index * (len(ordered) - 1) / max(1, sample_count - 1)) if ordered else 0
        available = [
            (abs(position - target), position, sample)
            for position, sample in enumerate(ordered)
            if not distinct_papers or (sample.run_id, sample.paper_id) not in used_papers
        ]
        if not available:
            break
        _, _, sample = min(available, key=lambda item: (item[0], item[1]))
        used_papers.add((sample.run_id, sample.paper_id))
        selected.append(replace(sample, sample_id=f"sample_{index + 1:02d}"))
    return selected


def run_sample(
    sample: Sample,
    *,
    harness: str,
    output_root: Path,
    model: str,
    provider: str,
    api_base: str,
    api_key_env: str,
    max_tokens: int,
    timeout_seconds: float,
    max_turns: int,
) -> dict[str, Any]:
    benchmark_id = f"benchmark_{sample.sample_id}_{harness.replace('-', '_')}"
    collector = ModelUsageCollector(
        network="benchmark",
        run_id=benchmark_id,
        batch_id=sample.sample_id,
    )
    base_config = FileAgentWorkflowConfig.from_env()
    config = replace(
        base_config,
        root=output_root / "workspaces" / harness,
        harness="hermes-cli",
        provider=provider,
        adjudication_harness="dspy" if harness == "dspy" else "file-agent",
        adjudication_provider=provider,
        adjudication_api_base=api_base,
        adjudication_api_key_env=api_key_env,
        adjudication_negative_model=model,
        adjudication_positive_model=model,
        adjudication_tiebreak_model=model,
        adjudication_batch_size=sample.case_count,
        adjudication_max_workers=1,
        adjudication_max_tokens=max_tokens,
        adjudication_timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
        resume_existing_stages=False,
    )
    session = FileAgentWorkflowSession(
        config=config,
        paper_id=sample.paper_id,
        workspace_id=benchmark_id,
        candidates=[],
        paper_context=dict(sample.task.get("paper") or {}),
        source_context_by_span_id=dict(sample.task.get("source_spans") or {}),
        usage_sink=collector.record,
    )
    outputs: dict[str, EligibilityAdjudicationAgentOutput] = {}
    errors: list[str] = []
    started = time.perf_counter()
    def run_primary(role: str) -> EligibilityAdjudicationAgentOutput:
        task = {**sample.task, "judge_role": role}
        return _run_role(
            session,
            role=role,
            task=task,
            model=model,
            stage_key=f"benchmark_{role}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            role: executor.submit(run_primary, role)
            for role in ("negative", "positive")
        }
        for role, future in futures.items():
            try:
                outputs[role] = future.result()
            except Exception as exc:
                errors.append(f"{role}: {type(exc).__name__}: {exc}")

    final_selections: dict[str, str | None] = {}
    disagreement_refs: list[str] = []
    if set(outputs) == {"negative", "positive"}:
        negative = {item.case_ref: item for item in outputs["negative"].assessments}
        positive = {item.case_ref: item for item in outputs["positive"].assessments}
        disagreement_refs = sorted(
            case_ref
            for case_ref in negative
            if negative[case_ref].selected_candidate_ref
            != positive[case_ref].selected_candidate_ref
        )
        for case_ref in negative:
            if case_ref not in disagreement_refs:
                final_selections[case_ref] = negative[case_ref].selected_candidate_ref
        if disagreement_refs:
            tiebreak_task = _tiebreak_task(
                sample.task,
                negative=negative,
                positive=positive,
                disagreement_refs=disagreement_refs,
            )
            try:
                tiebreak = _run_role(
                    session,
                    role="tiebreak",
                    task=tiebreak_task,
                    model=model,
                    stage_key="benchmark_tiebreak",
                )
                outputs["tiebreak"] = tiebreak
                final_selections.update(
                    {
                        item.case_ref: item.selected_candidate_ref
                        for item in tiebreak.assessments
                    }
                )
            except Exception as exc:
                errors.append(f"tiebreak: {type(exc).__name__}: {exc}")

    events = collector.snapshot()
    elapsed = time.perf_counter() - started
    summary = summarize_usage(events)
    return {
        **sample_metadata(sample),
        "harness": harness,
        "model": model,
        "provider": provider,
        "status": "complete" if not errors and len(final_selections) == sample.case_count else "failed",
        "errors": errors,
        "duration_seconds": round(elapsed, 3),
        "direct_agreement_count": sample.case_count - len(disagreement_refs),
        "tiebreak_case_count": len(disagreement_refs),
        "resolved_case_count": len(final_selections),
        "neither_count": sum(value is None for value in final_selections.values()),
        "selected_candidate_refs": final_selections,
        "usage": summary,
        "usage_events": events,
    }


def _run_role(
    session: FileAgentWorkflowSession,
    *,
    role: str,
    task: dict[str, Any],
    model: str,
    stage_key: str,
) -> EligibilityAdjudicationAgentOutput:
    payload = session._run_eligibility_stage_with_retry(
        stage_key=stage_key,
        stage_label=f"Benchmark eligibility adjudication {role} judge",
        model=model,
        task=task,
        output_model=EligibilityAdjudicationAgentOutput,
        skill_path=_skill_path(f"claims-silver-eligibility-selection-{role}"),
        validator=lambda result: _validate_eligibility_payload_for_task(result, task),
    )
    assert isinstance(payload, EligibilityAdjudicationAgentOutput)
    return payload


def _tiebreak_task(
    base_task: dict[str, Any],
    *,
    negative: dict[str, Any],
    positive: dict[str, Any],
    disagreement_refs: list[str],
) -> dict[str, Any]:
    cases = [
        case
        for case in base_task.get("cases", [])
        if isinstance(case, dict) and case.get("case_ref") in disagreement_refs
    ]
    return {
        **base_task,
        "judge_role": "tiebreak",
        "cases": cases,
        "primary_assessments": [
            {
                "case_ref": case_ref,
                "negative": negative[case_ref].model_dump(mode="json"),
                "positive": positive[case_ref].model_dump(mode="json"),
            }
            for case_ref in disagreement_refs
        ],
        "requirements": {
            **dict(base_task.get("requirements") or {}),
            "resolve_each_primary_disagreement": True,
            "return_only_disputed_cases": True,
        },
    }


def _base_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in task.items()
        if key not in {"judge_role", "operational_retry", "skill_instructions", "primary_assessments"}
    }


def summarize_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "call_count": len(events),
        "successful_call_count": 0,
        "failed_call_count": 0,
        "retry_call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "known_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "primary_cost_usd": 0.0,
        "recovery_cost_usd": 0.0,
        "by_runtime": {},
    }
    for event in events:
        status = str(event.get("status") or "")
        summary["successful_call_count" if status == "success" else "failed_call_count"] += 1
        is_retry = "retry" in str(event.get("stage_key") or "") or "_s" in str(event.get("stage_key") or "")
        if is_retry:
            summary["retry_call_count"] += 1
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "total_tokens",
        ):
            summary[key] += int(event.get(key) or 0)
        cost = float(event.get("cost_usd") or 0.0)
        summary["known_cost_usd"] += cost
        if event.get("cost_kind") == "actual":
            summary["actual_cost_usd"] += cost
        elif event.get("cost_kind") == "estimated":
            summary["estimated_cost_usd"] += cost
        summary["recovery_cost_usd" if is_retry else "primary_cost_usd"] += cost
        runtime = str(event.get("harness") or "unknown")
        runtime_row = summary["by_runtime"].setdefault(
            runtime,
            {"call_count": 0, "known_cost_usd": 0.0, "total_tokens": 0},
        )
        runtime_row["call_count"] += 1
        runtime_row["known_cost_usd"] += cost
        runtime_row["total_tokens"] += int(event.get("total_tokens") or 0)
    for key in (
        "known_cost_usd",
        "actual_cost_usd",
        "estimated_cost_usd",
        "primary_cost_usd",
        "recovery_cost_usd",
    ):
        summary[key] = round(summary[key], 8)
    for runtime_row in summary["by_runtime"].values():
        runtime_row["known_cost_usd"] = round(runtime_row["known_cost_usd"], 8)
    return summary


def compare_harnesses(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sample: dict[str, dict[str, dict[str, Any]]] = {}
    for row in results:
        by_sample.setdefault(str(row["sample_id"]), {})[str(row["harness"])] = row
    comparisons: list[dict[str, Any]] = []
    for sample_id, harness_rows in sorted(by_sample.items()):
        hermes = harness_rows.get("hermes-cli")
        dspy = harness_rows.get("dspy")
        if hermes is None or dspy is None:
            continue
        hermes_refs = dict(hermes.get("selected_candidate_refs") or {})
        dspy_refs = dict(dspy.get("selected_candidate_refs") or {})
        shared_refs = set(hermes_refs).intersection(dspy_refs)
        comparisons.append(
            {
                "sample_id": sample_id,
                "decision_agreement_count": sum(
                    hermes_refs[case_ref] == dspy_refs[case_ref]
                    for case_ref in shared_refs
                ),
                "shared_resolved_case_count": len(shared_refs),
                "hermes_cost_usd": hermes["usage"]["known_cost_usd"],
                "dspy_cost_usd": dspy["usage"]["known_cost_usd"],
                "hermes_duration_seconds": hermes["duration_seconds"],
                "dspy_duration_seconds": dspy["duration_seconds"],
                "hermes_retry_calls": hermes["usage"]["retry_call_count"],
                "dspy_retry_calls": dspy["usage"]["retry_call_count"],
            }
        )
    return comparisons


def aggregate_results(
    results: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> dict[str, Any]:
    by_harness: dict[str, dict[str, Any]] = {}
    for result in results:
        harness = str(result["harness"])
        usage = dict(result.get("usage") or {})
        row = by_harness.setdefault(
            harness,
            {
                "sample_count": 0,
                "duration_seconds": 0.0,
                "known_cost_usd": 0.0,
                "total_tokens": 0,
                "call_count": 0,
                "failed_call_count": 0,
                "retry_call_count": 0,
                "direct_agreement_count": 0,
                "tiebreak_case_count": 0,
                "neither_count": 0,
            },
        )
        row["sample_count"] += 1
        row["duration_seconds"] += float(result.get("duration_seconds") or 0.0)
        row["known_cost_usd"] += float(usage.get("known_cost_usd") or 0.0)
        for key in ("total_tokens", "call_count", "failed_call_count", "retry_call_count"):
            row[key] += int(usage.get(key) or 0)
        for key in ("direct_agreement_count", "tiebreak_case_count", "neither_count"):
            row[key] += int(result.get(key) or 0)
    for row in by_harness.values():
        row["duration_seconds"] = round(row["duration_seconds"], 3)
        row["known_cost_usd"] = round(row["known_cost_usd"], 8)

    hermes = by_harness.get("hermes-cli")
    dspy = by_harness.get("dspy")
    deltas: dict[str, float] = {}
    if hermes and dspy:
        for output_key, source_key in (
            ("dspy_cost_reduction_pct", "known_cost_usd"),
            ("dspy_duration_reduction_pct", "duration_seconds"),
            ("dspy_token_reduction_pct", "total_tokens"),
        ):
            baseline = float(hermes[source_key])
            if baseline:
                deltas[output_key] = round(
                    100.0 * (baseline - float(dspy[source_key])) / baseline,
                    3,
                )

    return {
        "by_harness": by_harness,
        "deltas": deltas,
        "cross_harness_decision_agreement_count": sum(
            int(item["decision_agreement_count"])
            for item in comparisons
        ),
        "cross_harness_shared_resolved_case_count": sum(
            int(item["shared_resolved_case_count"])
            for item in comparisons
        ),
    }


def sample_metadata(sample: Sample) -> dict[str, Any]:
    return {
        "sample_id": sample.sample_id,
        "source_run_id": sample.run_id,
        "paper_id": sample.paper_id,
        "source_path": str(sample.source_path),
        "input_bytes": sample.input_bytes,
        "case_count": sample.case_count,
        "singleton_count": sample.singleton_count,
        "pair_count": sample.pair_count,
    }


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "sample_id",
        "source_run_id",
        "paper_id",
        "harness",
        "status",
        "input_bytes",
        "case_count",
        "singleton_count",
        "pair_count",
        "resolved_case_count",
        "neither_count",
        "tiebreak_case_count",
        "duration_seconds",
        "call_count",
        "retry_call_count",
        "total_tokens",
        "cache_read_tokens",
        "known_cost_usd",
        "primary_cost_usd",
        "recovery_cost_usd",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            usage = result.get("usage") or {}
            writer.writerow(
                {
                    key: (
                        usage.get(key)
                        if key in usage
                        else result.get(key)
                    )
                    for key in fields
                }
            )


def _require_credentials(args: argparse.Namespace) -> None:
    if not os.getenv(args.api_key_env, "").strip():
        raise SystemExit(f"{args.api_key_env} is required for live benchmarking.")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
