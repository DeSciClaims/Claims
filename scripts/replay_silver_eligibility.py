#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from validator.agent_v1.adjudication_config import (
    SilverAdjudicationConfig,
    build_silver_adjudication_passes,
)
from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.diagnostic_batch import (
    DiagnosticBatchConfig,
    DiagnosticBatchSubmission,
    precomputed_rigor_manifest,
    run_diagnostic_batch,
)
from validator.agent_v1.file_agent_workflow import FileAgentSilverWorkflow
from validator.agent_v1.grounding import run_grounding_checks
from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline
from validator.agent_v1.comparison_models import SilverRecord
from validator.agent_v1.record_projection import project_agent_artifact
from validator.agent_v1.runner import AgentV1ValidatorRunner
from validator.agent_v1.silver_scoring import score_miner_against_silver
from validator.agent_v1.structural import run_structural_checks


def main() -> int:
    args = _parse_args()
    fixture_dir = args.fixture_dir.resolve()
    output_dir = args.output_dir.resolve() / args.condition
    output_dir.mkdir(parents=True, exist_ok=True)

    bronze_rows = _read_json(fixture_dir / "before-bronze-records.json")
    miner_rows = _read_json(fixture_dir / "miner-artifacts.json")
    if not isinstance(bronze_rows, list) or len(bronze_rows) != 1:
        raise SystemExit("Replay fixture must contain exactly one Bronze record.")
    if not isinstance(miner_rows, list) or not miner_rows:
        raise SystemExit("Replay fixture does not contain miner artifacts.")
    bronze = bronze_rows[0]
    bronze_artifact = _object(bronze.get("artifact"))
    bronze_source = _object(bronze.get("source_payload"))
    miner_rows = sorted(miner_rows, key=lambda row: int(row.get("uid") or 0))

    diagnostics = _run_diagnostics(
        miner_rows=miner_rows,
        output_dir=output_dir / "diagnostics",
        skip_rigor=args.condition == "skip-rigor",
        run_id=args.run_id,
        paper_id=args.paper_id,
    )
    if args.reuse_silver_result is not None:
        prior = _object(_read_json(args.reuse_silver_result.resolve()))
        silver_record = SilverRecord.model_validate(_object(prior.get("silver_record")))
        scores = [
            score_miner_against_silver(
                miner_id=f"uid_{int(row['uid'])}",
                miner_candidates=project_agent_artifact(
                    _object(row.get("agent_output")),
                    origin="miner",
                    miner_id=f"uid_{int(row['uid'])}",
                ),
                silver_record=silver_record,
                normal_findings=diagnostics[int(row["uid"])]["findings"],
            )
            for row in miner_rows
        ]
        payload = _replay_payload(
            args=args,
            miner_rows=miner_rows,
            diagnostics=diagnostics,
            eligibility_decisions=list(prior.get("eligibility_decisions") or []),
            silver_record=silver_record.model_dump(mode="json"),
            scores=scores,
            stage_timings=[
                {
                    "key": "silver_record_reuse",
                    "label": "Reuse completed eligibility Silver record",
                    "metadata": {"source_result": str(args.reuse_silver_result.resolve())},
                }
            ],
        )
        return _write_result(output_dir, payload)

    os.environ["CLAIMS_SILVER_ELIGIBILITY_ENABLE"] = "true"
    os.environ["CLAIMS_SILVER_FILE_WORKSPACE_ROOT"] = str(output_dir / "silver-workspaces")
    adjudication_config = SilverAdjudicationConfig.from_env(mode_default="hermes-cli")
    adjudication_passes, tiebreak_pass = build_silver_adjudication_passes(adjudication_config)
    workflow = FileAgentSilverWorkflow.from_env()
    result = run_paper_silver_pipeline(
        paper_id=args.paper_id,
        bronze_artifact=bronze_artifact,
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id=f"uid_{int(row['uid'])}",
                artifact=_object(row.get("agent_output")),
                claim_assessments=diagnostics[int(row["uid"])]["claim_assessments"],
            )
            for row in miner_rows
        ],
        silver_record_id=f"replay_{args.condition}_{args.run_id}_{args.paper_id}",
        bronze_record_id=str(bronze.get("bronze_record_id") or ""),
        adjudication_passes=adjudication_passes,
        tiebreak_pass=tiebreak_pass,
        paper_context=_paper_context(bronze_artifact),
        validation_findings_by_miner_id={
            f"uid_{uid}": payload["findings"]
            for uid, payload in diagnostics.items()
        },
        source_context=_source_context(bronze_source),
        source_context_by_span_id=_source_span_map(
            [bronze_source, *[_object(row.get("source_payload")) for row in miner_rows]]
        ),
        eligibility_source_context_by_span_id=_source_span_map([bronze_source]),
        adjudication_max_workers=max(
            1,
            int(os.getenv("CLAIMS_SILVER_ADJUDICATION_MAX_WORKERS", "4") or 4),
        ),
        adjudication_batch_size=max(
            1,
            int(os.getenv("CLAIMS_SILVER_ADJUDICATION_BATCH_SIZE", "8") or 8),
        ),
        max_eligible_claims_per_miner=max(
            1,
            int(os.getenv("CLAIMS_SILVER_MAX_ELIGIBLE_CLAIMS_PER_MINER", "1000") or 1000),
        ),
        filter_by_assessment=_env_flag("CLAIMS_SILVER_FILTER_BY_ASSESSMENT", False),
        max_adjudication_cases=max(
            1,
            int(os.getenv("CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER", "1000") or 1000),
        ),
        file_agent_workflow=workflow,
    )
    payload = _replay_payload(
        args=args,
        miner_rows=miner_rows,
        diagnostics=diagnostics,
        eligibility_decisions=[
            decision.model_dump(mode="json") for decision in result.eligibility_decisions
        ],
        silver_record=result.silver_record.model_dump(mode="json"),
        scores=result.scores,
        stage_timings=result.stage_timings,
    )
    return _write_result(output_dir, payload)


def _replay_payload(
    *,
    args: argparse.Namespace,
    miner_rows: list[dict[str, Any]],
    diagnostics: dict[int, dict[str, Any]],
    eligibility_decisions: list[dict[str, Any]],
    silver_record: dict[str, Any],
    scores: list[Any],
    stage_timings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "claims_silver_eligibility_replay_v1",
        "condition": args.condition,
        "source_run_id": args.run_id,
        "source_batch_id": args.batch_id,
        "paper_id": args.paper_id,
        "artifact_uids": [int(row["uid"]) for row in miner_rows],
        "diagnostics": {
            str(uid): {
                "finding_count": len(item["findings"]),
                "claim_assessment_count": len(item["claim_assessments"] or []),
            }
            for uid, item in diagnostics.items()
        },
        "eligibility_decisions": eligibility_decisions,
        "silver_record": silver_record,
        "scores": [score.model_dump(mode="json") for score in scores],
        "stage_timings": stage_timings,
        "summary": _result_summary(silver_record, scores),
    }


def _write_result(output_dir: Path, payload: dict[str, Any]) -> int:
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"result_path": str(result_path), **payload["summary"]}, indent=2))
    return 0


def _run_diagnostics(
    *,
    miner_rows: list[dict[str, Any]],
    output_dir: Path,
    skip_rigor: bool,
    run_id: str,
    paper_id: str,
) -> dict[int, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[int, Path, Path, DiagnosticBatchSubmission]] = []
    for index, row in enumerate(miner_rows, start=1):
        uid = int(row["uid"])
        row_dir = output_dir / f"uid_{uid}" / "input"
        row_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = row_dir / "agent_output.json"
        source_path = row_dir / "source_payload.json"
        artifact = _object(row.get("agent_output"))
        source_payload = _object(row.get("source_payload"))
        _write_json(artifact_path, artifact)
        _write_json(source_path, source_payload)
        _raw, normalized, structural = run_structural_checks(artifact_path)
        grounding = run_grounding_checks(normalized, source_payload)
        prepared.append(
            (
                uid,
                artifact_path,
                source_path,
                DiagnosticBatchSubmission(
                    submission_ref=f"S{index:04d}",
                    artifact=artifact,
                    source_payload=source_payload,
                    structural_findings=[item.model_dump(mode="json") for item in structural],
                    grounding_findings=[item.model_dump(mode="json") for item in grounding],
                ),
            )
        )

    precomputed: dict[str, dict[str, Any]] = {}
    precomputed_manifest: dict[str, Any] | None = None
    if not skip_rigor:
        batch_config = DiagnosticBatchConfig.from_env()
        batch_config = replace(
            batch_config,
            root=output_dir / "paper-workspace",
            batch_size=max(2, len(prepared)),
        )
        execution = run_diagnostic_batch(
            config=batch_config,
            run_id=f"replay_{run_id}",
            paper_id=paper_id,
            submissions=[item[3] for item in prepared],
        )
        if execution.error:
            raise RuntimeError(f"Diagnostic rigor batch failed: {execution.error}")
        precomputed = execution.reports
        precomputed_manifest = precomputed_rigor_manifest(execution)

    base_config = AgentV1ValidatorConfig.from_env()
    results: dict[int, dict[str, Any]] = {}
    for uid, artifact_path, source_path, submission in prepared:
        runner = AgentV1ValidatorRunner(
            base_config.model_copy(
                update={
                    "output_dir": output_dir / f"uid_{uid}",
                    "skip_rigor_agent": skip_rigor,
                    "validation_mode": "llm",
                }
            )
        )
        report = runner.run(
            artifact_path=artifact_path,
            source_payload_path=source_path,
            output_dir=output_dir / f"uid_{uid}" / "report",
            precomputed_rigor=(precomputed.get(submission.submission_ref) if not skip_rigor else None),
            precomputed_rigor_manifest=precomputed_manifest,
        )
        assessments = report.metadata.get("claim_assessments")
        results[uid] = {
            "findings": list(report.findings),
            "claim_assessments": assessments if isinstance(assessments, list) else None,
        }
    return results


def _result_summary(silver_record: dict[str, Any], scores: list[Any]) -> dict[str, Any]:
    role_counts = {"central": 0, "supporting": 0, "minor": 0}
    for unit in silver_record.get("silver_units", []):
        role = str(unit.get("importance") or "supporting")
        role_counts[role] = role_counts.get(role, 0) + 1
    score_rows = sorted(
        (
            {
                "miner_id": score.miner_id,
                "score": score.score,
                "coverage": score.coverage,
                "quality": score.quality,
            }
            for score in scores
        ),
        key=lambda row: (-row["score"], row["miner_id"]),
    )
    return {
        "silver_unit_count": len(silver_record.get("silver_units", [])),
        "role_counts": role_counts,
        "invalid_candidate_count": len(silver_record.get("invalid_miner_candidates", [])),
        "reference_error_count": len(silver_record.get("reference_errors", [])),
        "winner": score_rows[0] if score_rows else None,
        "scores": score_rows,
    }


def _paper_context(artifact: dict[str, Any]) -> dict[str, Any]:
    paper = _object(artifact.get("paper"))
    return {
        key: paper.get(key)
        for key in ("paper_id", "title", "abstract", "summary")
        if paper.get(key) not in (None, "")
    }


def _source_span_map(payloads: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for payload in payloads:
        spans = payload.get("spans")
        for index, span in enumerate(spans if isinstance(spans, list) else [], start=1):
            if not isinstance(span, dict):
                continue
            span_id = str(span.get("span_id") or span.get("id") or f"span_{index}").strip()
            text = str(span.get("text") or span.get("quote") or "").strip()
            if span_id and text:
                result[span_id] = text
    return result


def _source_context(payload: dict[str, Any]) -> str:
    return "\n".join(f"{span_id}: {text}" for span_id, text in _source_span_map([payload]).items())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay one paper through Silver eligibility and scoring.")
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--condition", choices=("skip-rigor", "with-rigor"), required=True)
    parser.add_argument(
        "--reuse-silver-result",
        type=Path,
        help="Reuse a completed replay Silver record and rerun only diagnostics plus scoring.",
    )
    parser.add_argument("--run-id", default="run_20260901_031335_be0dc6")
    parser.add_argument("--batch-id", default="batch_20260901_43288183d1b5")
    parser.add_argument("--paper-id", default="openalex_w4200227839")
    return parser.parse_args()


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
