from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import shlex
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator


CLAIMS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = CLAIMS_ROOT.parent
BACKEND_ROOT = WORKSPACE_ROOT / "Claims-Backend-Service"
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))
PAPER_ID = "rietveld_et_al_2013_science"
PAPER_TITLE = "GWAS of 126,559 Individuals Identifies Genetic Variants Associated with Educational Attainment"
BRONZE_ROOT = CLAIMS_ROOT / "validator" / "agent_v1" / "bronze" / "production" / PAPER_ID
PDF_NAME = "9ab89ecfc737f0d333ce70a698c3e76874a4c7d9e1d82dc3324974b1b9015890.pdf"
DEFAULT_PDF = BRONZE_ROOT / "data" / PDF_NAME
NETWORK = "local-benchmark"
LOCAL_UID = 999
LOCAL_HOTKEY = "local_file_agent_miner"


def main() -> int:
    args = _parse_args()
    _load_dotenv(CLAIMS_ROOT / ".env")
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    db_path = output_root / "claims_backend.sqlite3"
    summary_path = output_root / "benchmark_summary.json"

    harnesses = [_normalize_harness(item) for item in args.harnesses.split(",") if item.strip()]
    unknown = sorted(set(harnesses).difference({"hermes-cli", "codex-cli", "claude-cli"}))
    if unknown:
        raise SystemExit(f"Unknown harnesses: {', '.join(unknown)}")

    print(f"Benchmark output: {output_root}", flush=True)
    print(f"Harnesses: {', '.join(harnesses)}", flush=True)
    preflight = {
        harness: ({"status": "ready", "detail": "local deterministic fixture"} if args.local_stub else _harness_preflight(harness))
        for harness in harnesses
    }
    _write_json(output_root / "harness_preflight.json", preflight)
    for harness, result in preflight.items():
        print(f"{harness}: {result['status']} ({result['detail']})", flush=True)

    miner_dir = output_root / "miner"
    if args.reuse_miner and _miner_files_exist(miner_dir):
        print(f"Reusing miner artifact at {miner_dir}", flush=True)
    else:
        _run_miner(
            pdf_path=args.pdf.resolve(),
            output_dir=miner_dir,
            model=args.miner_model,
            timeout_seconds=args.timeout,
        )

    miner_artifact = _read_json(miner_dir / "agent_output.json")
    miner_source_payload = _read_json(miner_dir / "source_payload.json")
    bronze_artifact = _read_json(BRONZE_ROOT / "agent_output.json")
    bronze_source_payload = _read_json(BRONZE_ROOT / "source_payload.json")
    _require_pdf_inspector(miner_source_payload, label="miner")
    _require_pdf_inspector(bronze_source_payload, label="Bronze")

    diagnostic_report = _run_diagnostic(
        miner_dir=miner_dir,
        output_dir=output_root / "diagnostic",
        threshold=args.validation_threshold,
        mode=args.diagnostic_mode,
        model=args.rigor_model,
        timeout_seconds=args.timeout,
    )
    _write_json(output_root / "diagnostic_report.json", diagnostic_report.model_dump(mode="json"))

    existing_summary = _read_json(summary_path) if summary_path.exists() else {}
    benchmark: dict[str, Any] = {
        "schema": "claims_live_file_agent_benchmark_v1",
        "created_at": existing_summary.get("created_at") or _now(),
        "updated_at": _now(),
        "paper_id": PAPER_ID,
        "pdf_reader": "pdf-inspector",
        "miner": {
            "artifact_path": str(miner_dir / "agent_output.json"),
            "source_payload_path": str(miner_dir / "source_payload.json"),
            "model": args.miner_model,
            "claim_count": len(miner_artifact.get("logic", {}).get("claims", [])),
            "validation_score": diagnostic_report.score,
            "validation_findings": len(diagnostic_report.findings),
            "diagnostic_mode": args.diagnostic_mode,
            "rigor_model": args.rigor_model if args.diagnostic_mode == "live" else None,
        },
        "backend": {
            **(existing_summary.get("backend") if isinstance(existing_summary.get("backend"), dict) else {}),
            "database_path": str(db_path),
        },
        "harnesses": dict(existing_summary.get("harnesses") or {}),
    }
    _write_json(summary_path, benchmark)

    with _sqlite_backend(db_path) as backend:
        client, base_url = backend
        benchmark["backend"]["base_url"] = base_url
        for harness in harnesses:
            availability = preflight[harness]
            if availability["status"] != "ready":
                benchmark["harnesses"][harness] = {
                    "status": "unavailable",
                    "error": availability["detail"],
                }
                _write_json(summary_path, benchmark)
                if not args.skip_unavailable:
                    raise RuntimeError(f"{harness} unavailable: {availability['detail']}")
                continue

            model = {
                "hermes-cli": args.hermes_model,
                "codex-cli": args.codex_model,
                "claude-cli": args.claude_model,
            }[harness]
            print(f"Running {harness} file-agent workflow with {model}", flush=True)
            result = _run_harness_benchmark(
                client=client,
                base_url=base_url,
                output_root=output_root,
                harness=harness,
                model=model,
                timeout_seconds=args.timeout,
                bronze_artifact=bronze_artifact,
                bronze_source_payload=bronze_source_payload,
                miner_artifact=miner_artifact,
                miner_source_payload=miner_source_payload,
                diagnostic_report=diagnostic_report,
                local_stub=args.local_stub,
            )
            benchmark["harnesses"][harness] = result
            _write_json(summary_path, benchmark)

    print(f"Benchmark summary: {summary_path}", flush=True)
    failed = [name for name, row in benchmark["harnesses"].items() if row.get("status") == "failed"]
    return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(
        description="Run one live Rietveld miner artifact through file-agent Silver workflows and a local SQLite backend."
    )
    parser.add_argument("--output-root", type=Path, default=CLAIMS_ROOT / "outputs" / "file_agent_benchmark" / stamp)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--harnesses", default="hermes-cli,codex-cli,claude-cli")
    parser.add_argument("--miner-model", default="openai/gpt-5-mini")
    parser.add_argument("--hermes-model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--codex-model", default="gpt-5.5")
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--validation-threshold", type=float, default=0.0)
    parser.add_argument("--diagnostic-mode", choices=("live", "deterministic"), default="live")
    parser.add_argument("--rigor-model", default="openai/gpt-4o-mini")
    parser.add_argument("--reuse-miner", action="store_true")
    parser.add_argument(
        "--local-stub",
        action="store_true",
        help="Use the deterministic local file-agent fixture instead of contacting a model service.",
    )
    parser.add_argument("--skip-unavailable", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def _run_miner(*, pdf_path: Path, output_dir: Path, model: str, timeout_seconds: float) -> None:
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    from miner.agent_v1.config import AgentV1Config
    from miner.agent_v1.runner import AgentV1Runner

    output_dir.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    config = AgentV1Config.from_env(CLAIMS_ROOT).model_copy(
        update={
            "runtime": "agent-cli",
            "pdf_reader": "pdf-inspector",
            "model": model,
            "timeout_seconds": int(timeout_seconds),
            "output_dir": output_dir,
            "cli_command": [str(python), "-m", "miner.agent_v1.wrappers.hermes_prompt"],
        }
    )
    inner_command = f"hermes chat --provider openrouter -m {model} --max-turns 30 -q"
    print(f"Generating one miner artifact with Hermes/{model} and pdf-inspector", flush=True)
    with _temporary_env(
        {
            "CLAIMS_AGENT_INNER_COMMAND": inner_command,
            "CLAIMS_AGENT_EXIT_ON_VALID_OUTPUT": "true",
            "SUBNET_CLAIMS_PDF_READER": "pdf-inspector",
        }
    ):
        AgentV1Runner(config).run_from_pdf(
            pdf_path,
            output_dir=output_dir,
            paper_override={"paper_id": PAPER_ID, "title": PAPER_TITLE},
        )
    if not _miner_files_exist(output_dir):
        raise RuntimeError("Miner run completed without agent_output.json and source_payload.json.")


def _run_diagnostic(
    *,
    miner_dir: Path,
    output_dir: Path,
    threshold: float,
    mode: str,
    model: str,
    timeout_seconds: float,
):
    from validator.agent_v1.config import AgentV1ValidatorConfig
    from validator.agent_v1.runner import AgentV1ValidatorRunner

    if mode == "deterministic":
        config = AgentV1ValidatorConfig.from_env(CLAIMS_ROOT).model_copy(
            update={"skip_rigor_agent": True, "validation_mode": "deterministic"}
        )
        print("Running deterministic structural and grounding diagnostics once", flush=True)
        return AgentV1ValidatorRunner(config).run(
            artifact_path=miner_dir / "agent_output.json",
            source_payload_path=miner_dir / "source_payload.json",
            output_dir=output_dir,
            threshold=threshold,
        )

    config = AgentV1ValidatorConfig.from_env(CLAIMS_ROOT).model_copy(
        update={
            "runtime": "agent-cli",
            "model": model,
            "skip_rigor_agent": False,
            "validation_mode": "llm",
            "timeout_seconds": int(timeout_seconds),
            "cli_command": [sys.executable, "-m", "validator.agent_v1.wrappers.hermes_prompt"],
        }
    )
    inner_command = f"hermes chat --provider openrouter -m {model} --max-turns 30 -q"
    print(f"Running live diagnostic validation with Hermes/{model}", flush=True)
    with _temporary_env(
        {
            "CLAIMS_VALIDATOR_AGENT_INNER_COMMAND": inner_command,
            "CLAIMS_VALIDATOR_AGENT_PROMPT_MODE": "append",
            "CLAIMS_VALIDATOR_AGENT_INNER_TIMEOUT": str(int(timeout_seconds)),
        }
    ):
        return AgentV1ValidatorRunner(config).run(
            artifact_path=miner_dir / "agent_output.json",
            source_payload_path=miner_dir / "source_payload.json",
            output_dir=output_dir,
            threshold=threshold,
        )


def _run_harness_benchmark(
    *,
    client,
    base_url: str,
    output_root: Path,
    harness: str,
    model: str,
    timeout_seconds: float,
    bronze_artifact: dict[str, Any],
    bronze_source_payload: dict[str, Any],
    miner_artifact: dict[str, Any],
    miner_source_payload: dict[str, Any],
    diagnostic_report,
    local_stub: bool,
) -> dict[str, Any]:
    from validator.agent_v1.adjudication_config import SilverAdjudicationConfig, build_silver_adjudication_passes
    from validator.agent_v1.file_agent_workflow import FileAgentSilverWorkflow, FileAgentWorkflowConfig
    from validator.agent_v1.model_usage import ModelUsageCollector
    from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    slug = harness.removesuffix("-cli").replace("-", "_")
    run_id = f"local_file_agent_{slug}_{stamp}"
    task_id = f"task_{run_id}"
    batch_id = f"batch_{run_id}"
    bronze_record_id = f"bronze_{run_id}_{PAPER_ID}"
    silver_record_id = f"silver_{run_id}_{PAPER_ID}"
    response_id = f"{run_id}:uid_{LOCAL_UID}"
    started_at = _now()
    run_dir = output_root / "runs" / harness
    run_dir.mkdir(parents=True, exist_ok=True)

    run_payload = {
        "run_id": run_id,
        "network": NETWORK,
        "task_id": task_id,
        "batch_id": batch_id,
        "validator_hotkey": client.wallet.hotkey.ss58_address,
        "target_uids": [LOCAL_UID],
        "status": "running",
        "started_at": started_at,
        "metadata": {
            "benchmark": "live_file_agent_v1",
            "paper_ids": [PAPER_ID],
            "workflow_mode": "file-agent",
            "harness": harness,
            "model": model,
            "pdf_reader": "pdf-inspector",
        },
    }
    client.post("/validator/runs", run_payload)
    _persist_inputs(
        client=client,
        run_id=run_id,
        batch_id=batch_id,
        bronze_record_id=bronze_record_id,
        response_id=response_id,
        bronze_artifact=bronze_artifact,
        bronze_source_payload=bronze_source_payload,
        miner_artifact=miner_artifact,
        diagnostic_report=diagnostic_report,
    )

    collector = ModelUsageCollector(network=NETWORK, run_id=run_id, batch_id=batch_id)
    stub_command = [sys.executable, str(CLAIMS_ROOT / "tests" / "fixtures" / "file_agent_workspace_stub.py")]
    stub_command_text = shlex.join(stub_command) if local_stub else ""
    config = SilverAdjudicationConfig(
        mode=harness,
        api_base=os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
        api_key_env="OPENROUTER_API_KEY",
        model_a=model,
        model_b=model,
        tiebreak_model=model,
        cli_timeout_seconds=timeout_seconds,
        cli_provider="openrouter",
        cli_max_turns=30,
        max_tokens=81920,
        timeout_seconds=timeout_seconds,
        retries=0,
        max_in_flight=4,
        cli_command_a=stub_command_text,
        cli_command_b=stub_command_text,
        cli_tiebreak_command=stub_command_text,
    )
    passes, tiebreak = build_silver_adjudication_passes(config, usage_sink=collector.record)
    request_gate = getattr(passes[0], "request_gate", None) if passes else None
    file_workflow = FileAgentSilverWorkflow(
        config=FileAgentWorkflowConfig(
            root=output_root / "workspaces",
            harness=harness,
            provider="openrouter" if harness == "hermes-cli" else "first-party",
            comparison_model=model,
            canonicalization_model=model,
            max_turns=30,
            timeout_seconds=timeout_seconds,
            output_poll_seconds=0.5,
            output_stable_seconds=1.0,
            fallback_to_legacy=False,
            command_template=stub_command_text,
        ),
        usage_sink=collector.record,
        request_gate=request_gate,
    )
    source_context_by_span_id = _source_context_map(bronze_source_payload, miner_source_payload)
    source_context = "\n\n".join(source_context_by_span_id.values())
    paper_context = dict(bronze_artifact.get("paper") or {})
    validation_findings = {f"uid_{LOCAL_UID}": list(diagnostic_report.findings)}

    started = time.perf_counter()
    try:
        pipeline = run_paper_silver_pipeline(
            paper_id=PAPER_ID,
            bronze_artifact=bronze_artifact,
            miner_artifacts=[MinerArtifactSubmission(miner_id=f"uid_{LOCAL_UID}", artifact=miner_artifact)],
            silver_record_id=silver_record_id,
            bronze_record_id=bronze_record_id,
            adjudication_passes=passes,
            tiebreak_pass=tiebreak,
            direct_judge_confidence=0.9,
            paper_context=paper_context,
            validation_findings_by_miner_id=validation_findings,
            source_context=source_context,
            source_context_by_span_id=source_context_by_span_id,
            adjudication_max_workers=3,
            adjudication_batch_size=1000,
            file_agent_workflow=file_workflow,
        )
        elapsed = time.perf_counter() - started
        _write_json(run_dir / "pipeline_result.json", _pipeline_json(pipeline))
        persisted = _persist_pipeline(
            client=client,
            run_id=run_id,
            batch_id=batch_id,
            bronze_record_id=bronze_record_id,
            response_id=response_id,
            pipeline=pipeline,
        )
        usage_events = collector.snapshot()
        usage_status = client.post_model_usage_events(usage_events) if usage_events else {
            "expected_event_count": 0,
            "stored_event_count": 0,
            "complete": True,
        }
        client.post(
            "/validator/runs",
            {
                **run_payload,
                "status": "completed",
                "ended_at": _now(),
                "metadata": {
                    **run_payload["metadata"],
                    "paper_count": 1,
                    "stage_timings": pipeline.stage_timings,
                    "elapsed_seconds": round(elapsed, 3),
                    "usage_event_count": len(usage_events),
                    "local_stub": local_stub,
                },
            },
        )
        verification = _verify_backend(client, base_url=base_url, run_id=run_id)
        score = pipeline.scores[0] if pipeline.scores else None
        return {
            "status": "completed",
            "run_id": run_id,
            "harness": harness,
            "model": model,
            "local_stub": local_stub,
            "elapsed_seconds": round(elapsed, 3),
            "comparison_edges": len(pipeline.candidate_graph_edges),
            "adjudication_cases": len(pipeline.diff_cases),
            "silver_units": len(pipeline.silver_record.silver_units),
            "coverage": score.coverage if score else None,
            "quality": score.quality if score else None,
            "score": score.score if score else None,
            "usage_events": len(usage_events),
            "usage_status": usage_status,
            "persisted": persisted,
            "verification": verification,
            "result_path": str(run_dir / "pipeline_result.json"),
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        usage_events = collector.snapshot()
        usage_status: dict[str, Any] = {}
        if usage_events:
            try:
                usage_status = client.post_model_usage_events(usage_events)
            except Exception as usage_exc:
                usage_status = {"error": f"{type(usage_exc).__name__}: {usage_exc}"}
        error = f"{type(exc).__name__}: {exc}"
        _write_json(run_dir / "failure.json", {"error": error, "elapsed_seconds": elapsed})
        client.post(
            "/validator/runs",
            {
                **run_payload,
                "status": "failed",
                "ended_at": _now(),
                "error_summary": error,
                "metadata": {
                    **run_payload["metadata"],
                    "elapsed_seconds": round(elapsed, 3),
                    "usage_event_count": len(usage_events),
                    "local_stub": local_stub,
                },
            },
        )
        print(f"{harness} failed: {error}", flush=True)
        return {
            "status": "failed",
            "run_id": run_id,
            "harness": harness,
            "model": model,
            "local_stub": local_stub,
            "elapsed_seconds": round(elapsed, 3),
            "usage_events": len(usage_events),
            "usage_status": usage_status,
            "error": error,
            "failure_path": str(run_dir / "failure.json"),
        }


def _persist_inputs(
    *,
    client,
    run_id: str,
    batch_id: str,
    bronze_record_id: str,
    response_id: str,
    bronze_artifact: dict[str, Any],
    bronze_source_payload: dict[str, Any],
    miner_artifact: dict[str, Any],
    diagnostic_report,
) -> None:
    client.post(
        "/validator/miner-responses",
        {
            "response_id": response_id,
            "network": NETWORK,
            "run_id": run_id,
            "uid": LOCAL_UID,
            "hotkey": LOCAL_HOTKEY,
            "batch_id": batch_id,
            "response_hash": _json_hash(miner_artifact),
            "schema_version": "agent_v1",
            "miner_version": "local-live-benchmark",
            "backend": "local-file",
            "status": "completed",
            "received_at": _now(),
        },
    )
    client.post(
        "/validator/validation-reports",
        {
            "report_id": f"validation_{run_id}_uid_{LOCAL_UID}",
            "network": NETWORK,
            "response_id": response_id,
            "run_id": run_id,
            "uid": LOCAL_UID,
            "hotkey": LOCAL_HOTKEY,
            "score": diagnostic_report.score,
            "threshold": diagnostic_report.threshold,
            "passed": diagnostic_report.passed,
            "summary": (
                diagnostic_report.summary.model_dump(mode="json")
                if hasattr(diagnostic_report.summary, "model_dump")
                else dict(diagnostic_report.summary)
            ),
            "report_uri": f"claims-api:/runs/{run_id}/validation/uid_{LOCAL_UID}",
            "findings": [finding.model_dump(mode="json") for finding in diagnostic_report.findings],
            "paper_scores": [{"paper_id": PAPER_ID, "score": diagnostic_report.score}],
        },
    )
    client.post_bronze_record(
        {
            "bronze_record_id": bronze_record_id,
            "network": NETWORK,
            "run_id": run_id,
            "batch_id": batch_id,
            "paper_id": PAPER_ID,
            "reference_release_id": "local-reference-pdf-inspector",
            "reference_profile_id": "local-existing-reference-artifact",
            "model_runtime_id": str(bronze_artifact.get("metadata", {}).get("runtime") or "agent-cli"),
            "artifact_hash": _json_hash(bronze_artifact),
            "artifact_uri": f"claims-api:/bronze-records/{bronze_record_id}/artifact",
            "source_payload_uri": f"claims-api:/bronze-records/{bronze_record_id}/source-payload",
            "artifact": bronze_artifact,
            "source_payload": bronze_source_payload,
            "metadata": {"pdf_reader": "pdf-inspector", "benchmark": "live_file_agent_v1"},
            "status": "created",
        }
    )


def _persist_pipeline(*, client, run_id: str, batch_id: str, bronze_record_id: str, response_id: str, pipeline) -> dict[str, Any]:
    backend_case_ids = {case.case_id: f"{run_id}_{case.case_id}" for case in pipeline.diff_cases}
    cases = []
    for case in pipeline.diff_cases:
        backend_case_id = backend_case_ids[case.case_id]
        case_json = case.model_dump(mode="json")
        case_json["original_case_id"] = case.case_id
        case_json["case_id"] = backend_case_id
        cases.append(
            {
                "case_id": backend_case_id,
                "network": NETWORK,
                "run_id": run_id,
                "batch_id": batch_id,
                "paper_id": PAPER_ID,
                "bronze_record_id": bronze_record_id,
                "miner_hotkey": LOCAL_HOTKEY,
                "uid": LOCAL_UID,
                "mismatch_type": case.mismatch_type,
                "candidate_ids": case.candidate_ids,
                "status": "resolved",
                "context_uri": f"claims-api:/runs/{run_id}/papers/{PAPER_ID}/adjudication-consensus",
                "decision": {"case": case_json},
            }
        )

    votes = []
    consensus_rows = []
    for consensus in pipeline.adjudication_consensus:
        backend_case_id = backend_case_ids[consensus.case_id]
        for vote in consensus.votes:
            votes.append(
                {
                    "vote_id": f"{backend_case_id}_{vote.pass_id}",
                    "network": NETWORK,
                    "case_id": backend_case_id,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": PAPER_ID,
                    "pass_id": vote.pass_id,
                    "adjudication_profile_id": vote.adjudication_profile_id,
                    "model_runtime_id": vote.model_runtime_id,
                    "candidate_order_seed": vote.case_id,
                    "disposition": vote.disposition,
                    "material_findings": vote.material_findings,
                    "cited_span_ids": vote.cited_span_ids,
                    "confidence": vote.confidence,
                    "rationale": vote.rationale,
                }
            )
        consensus_rows.append(
            {
                "consensus_id": f"{backend_case_id}_consensus",
                "network": NETWORK,
                "case_id": backend_case_id,
                "run_id": run_id,
                "batch_id": batch_id,
                "paper_id": PAPER_ID,
                "final_disposition": consensus.final_disposition,
                "final_confidence": consensus.final_confidence,
                "agreement_rate": consensus.agreement_rate,
                "route": consensus.route,
                "score_effects": [item.model_dump(mode="json") for item in consensus.score_effects],
            }
        )

    decisions = []
    for decision in pipeline.adjudication_decisions:
        backend_case_id = backend_case_ids[decision.case_id]
        decision_json = decision.model_dump(mode="json")
        decision_json["original_case_id"] = decision.case_id
        decision_json["case_id"] = backend_case_id
        decisions.append(
            {
                "decision_id": f"{backend_case_id}_decision",
                "network": NETWORK,
                "case_id": backend_case_id,
                "run_id": run_id,
                "batch_id": batch_id,
                "paper_id": PAPER_ID,
                "disposition": decision.disposition,
                "accepted_candidate_ids": decision.accepted_candidate_ids,
                "rejected_candidate_ids": decision.rejected_candidate_ids,
                "valid_alternative_candidate_ids": decision.valid_alternative_candidate_ids,
                "silver_unit_id": decision.silver_unit_id,
                "creates_required_silver_unit": decision.creates_required_silver_unit,
                "creates_optional_improvement_unit": decision.creates_optional_improvement_unit,
                "importance": decision.importance,
                "rationale": decision.rationale,
                "decision": decision_json,
            }
        )

    silver_units = []
    for unit in pipeline.silver_record.silver_units:
        payload = unit.model_dump(mode="json")
        payload["adjudication_case_ids"] = [backend_case_ids.get(case_id, case_id) for case_id in unit.adjudication_case_ids]
        silver_units.append(payload)
    invalid_candidates = []
    for item in pipeline.silver_record.invalid_miner_candidates:
        payload = item.model_dump(mode="json")
        if payload.get("adjudication_case_id"):
            payload["adjudication_case_id"] = backend_case_ids.get(payload["adjudication_case_id"], payload["adjudication_case_id"])
        invalid_candidates.append(payload)
    silver_records = [
        {
            "silver_record_id": pipeline.silver_record.silver_record_id,
            "network": NETWORK,
            "run_id": run_id,
            "batch_id": batch_id,
            "paper_id": PAPER_ID,
            "bronze_record_id": bronze_record_id,
            "silver_units": silver_units,
            "invalid_candidates": invalid_candidates,
            "reference_errors": [item.model_dump(mode="json") for item in pipeline.silver_record.reference_errors],
            "metadata": pipeline.silver_record.metadata,
            "audit_uri": f"claims-api:/runs/{run_id}/papers/{PAPER_ID}/silver-record",
        }
    ]
    scores = [
        {
            "score_report_id": f"silver_score_{run_id}_{PAPER_ID}_uid_{LOCAL_UID}",
            "network": NETWORK,
            "run_id": run_id,
            "batch_id": batch_id,
            "paper_id": PAPER_ID,
            "response_id": response_id,
            "uid": LOCAL_UID,
            "hotkey": LOCAL_HOTKEY,
            "silver_record_id": score.silver_record_id,
            "coverage": score.coverage,
            "quality": score.quality,
            "score": score.score,
            "findings": [finding.model_dump(mode="json") for finding in score.findings],
            "breakdown": score.model_dump(mode="json"),
        }
        for score in pipeline.scores
    ]
    return client.post_silver_pipeline_chunks(
        cases=cases,
        votes=votes,
        consensus=consensus_rows,
        decisions=decisions,
        silver_records=silver_records,
        score_reports=scores,
    )


def _verify_backend(client, *, base_url: str, run_id: str) -> dict[str, Any]:
    detail = client.get(f"/public/runs/{run_id}")
    silver = client.get(f"/public/runs/{run_id}/silver-records", query={"network": NETWORK})
    scores = client.get(f"/public/runs/{run_id}/silver-scores", query={"network": NETWORK})
    bronze = client.get(f"/public/runs/{run_id}/bronze-records", query={"network": NETWORK})
    usage = client.get(
        f"/public/runs/{run_id}/model-usage",
        query={"network": NETWORK, "include_events": "true"},
    )
    if not silver or not scores or not bronze:
        raise RuntimeError(
            f"Backend verification failed for {run_id}: bronze={len(bronze)} silver={len(silver)} scores={len(scores)}"
        )
    return {
        "run_status": detail.get("status"),
        "bronze_records": len(bronze),
        "silver_records": len(silver),
        "score_reports": len(scores),
        "usage_events": int(usage.get("event_count") or 0),
        "public_run_url": f"{base_url}/public/runs/{run_id}",
    }


@contextmanager
def _sqlite_backend(db_path: Path) -> Iterator[tuple[Any, str]]:
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))
    from bittensor_wallet import Keypair
    import uvicorn

    keypair = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    with _temporary_env(
        {
            "CLAIMS_STORAGE_BACKEND": "sqlite",
            "CLAIMS_DATABASE_PATH": str(db_path),
            "CLAIMS_VALIDATOR_HOTKEY_ALLOWLIST": keypair.ss58_address,
        }
    ):
        from app.config import get_settings
        from app.db import init_db
        from app.main import app
        from neurons.backend_client import ClaimsBackendClient

        get_settings.cache_clear()
        init_db()
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _wait_for_port(port)
        base_url = f"http://127.0.0.1:{port}"
        wallet = SimpleNamespace(hotkey=keypair)
        client = ClaimsBackendClient(base_url=base_url, wallet=wallet, network=NETWORK, timeout_seconds=120, max_retries=2)
        try:
            health = client.get("/health")
            if health.get("storage_backend") != "sqlite":
                raise RuntimeError(f"Unexpected backend health payload: {health}")
            yield client, base_url
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def _harness_preflight(harness: str) -> dict[str, str]:
    executable = {"hermes-cli": "hermes", "codex-cli": "codex", "claude-cli": "claude"}[harness]
    path = shutil.which(executable)
    if not path:
        return {"status": "unavailable", "detail": f"{executable} is not on PATH"}
    if harness == "hermes-cli":
        if not os.getenv("OPENROUTER_API_KEY"):
            return {"status": "unavailable", "detail": "OPENROUTER_API_KEY is not set"}
        return {"status": "ready", "detail": path}
    command = [path, "login", "status"] if harness == "codex-cli" else [path, "auth", "status"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    combined = f"{completed.stdout}\n{completed.stderr}".strip()
    if harness == "codex-cli" and completed.returncode == 0 and "logged in" in combined.lower():
        return {"status": "ready", "detail": path}
    if harness == "claude-cli" and completed.returncode == 0:
        try:
            if bool(json.loads(completed.stdout).get("loggedIn")):
                return {"status": "ready", "detail": path}
        except (json.JSONDecodeError, AttributeError):
            pass
    detail = combined.replace("\n", " ")[:300] or f"{executable} authentication check failed"
    return {"status": "unavailable", "detail": detail}


def _source_context_map(*payloads: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for payload in payloads:
        for span in payload.get("spans", []):
            if not isinstance(span, dict):
                continue
            text = str(span.get("text") or "").strip()
            if not text:
                continue
            span_id = str(span.get("span_id") or "").strip()
            if span_id:
                result[span_id] = text
            metadata = span.get("metadata") if isinstance(span.get("metadata"), dict) else {}
            reader_span_id = str(metadata.get("reader_span_id") or "").strip()
            if reader_span_id:
                result[reader_span_id] = text
    return result


def _pipeline_json(pipeline) -> dict[str, Any]:
    return {
        "paper_id": pipeline.paper_id,
        "bronze_candidates": [item.model_dump(mode="json") for item in pipeline.bronze_candidates],
        "miner_submissions": [
            {
                "miner_id": item.miner_id,
                "paper_id": item.paper_id,
                "candidates": [candidate.model_dump(mode="json") for candidate in item.candidates],
            }
            for item in pipeline.miner_submissions
        ],
        "candidate_graph_edges": [item.model_dump(mode="json") for item in pipeline.candidate_graph_edges],
        "diff_cases": [item.model_dump(mode="json") for item in pipeline.diff_cases],
        "adjudication_consensus": [item.model_dump(mode="json") for item in pipeline.adjudication_consensus],
        "adjudication_decisions": [item.model_dump(mode="json") for item in pipeline.adjudication_decisions],
        "silver_record": pipeline.silver_record.model_dump(mode="json"),
        "scores": [item.model_dump(mode="json") for item in pipeline.scores],
        "stage_timings": pipeline.stage_timings,
    }


def _require_pdf_inspector(payload: dict[str, Any], *, label: str) -> None:
    readers = {
        str(span.get("metadata", {}).get("pdf_reader") or "")
        for span in payload.get("spans", [])
        if isinstance(span, dict) and isinstance(span.get("metadata"), dict)
    }
    if readers != {"pdf-inspector"}:
        raise RuntimeError(f"{label} source payload is not uniformly pdf-inspector: {sorted(readers)}")


def _miner_files_exist(path: Path) -> bool:
    return (path / "agent_output.json").is_file() and (path / "source_payload.json").is_file()


def _normalize_harness(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    return value if value.endswith("-cli") else f"{value}-cli"


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _load_dotenv(path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


@contextmanager
def _temporary_env(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"Local backend did not start on port {port}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
