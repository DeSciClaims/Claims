"""Paired end-to-end compact/decision replay of saved testnet papers.

Frozen extraction/reference inputs; fresh diagnostics, comparison, eligibility,
canonical draft/audit. No backend clients, wallet, or weight writes.
"""

from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import traceback

from dotenv import load_dotenv
from scripts.benchmark_equivalence_routing import (
    fixture_inputs,
    disable_response_cache,
    read,
    write,
)
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    FileAgentSilverWorkflow,
)
from validator.agent_v1.model_usage import ModelUsageCollector
from validator.agent_v1.orchestrator import run_paper_silver_pipeline
from validator.agent_v1.diagnostic_batch import (
    DiagnosticBatchConfig,
    DiagnosticBatchSubmission,
    run_diagnostic_batch,
    precomputed_rigor_manifest,
)
from validator.agent_v1.structural import run_structural_checks
from validator.agent_v1.grounding import run_grounding_checks
from validator.agent_v1.runner import AgentV1ValidatorRunner
from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.silver_scoring import score_miner_against_silver


def stage_name(key):
    if "diagnostic" in key:
        return "diagnostics"
    if "comparison" in key:
        return "comparison"
    if "blind_reconstruction" in key:
        return "blind_reconstruction"
    if "tiebreak" in key:
        return "tiebreak"
    if "positive" in key:
        return "positive_judge"
    if "negative" in key:
        return "negative_judge"
    if "canonicalization_draft" in key:
        return "canonical_draft"
    if "canonicalization_audit" in key:
        return "canonical_audit"
    return key


def summarize(events):
    groups = defaultdict(
        lambda: dict(
            events=0,
            failed=0,
            missing=0,
            prompt_tokens=0,
            completion_tokens=0,
            reasoning_tokens=0,
            total_tokens=0,
            cost_usd=0.0,
        )
    )
    for e in events:
        for name in [stage_name(e["stage_key"]), "all"]:
            g = groups[name]
            g["events"] += 1
            g["failed"] += e["status"] != "success"
            g["missing"] += (
                e.get("completion_tokens") is None or e.get("total_tokens") is None
            )
            for k in [
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
                "cost_usd",
            ]:
                g[k] += e.get(k) or 0
    return dict(groups)


def ranks(scores):
    # Competition ranks; equal rounded scores share a rank.
    return {
        mid: 1 + sum(other > score for other in scores.values())
        for mid, score in scores.items()
    }


def audit_usage(events):
    rows = []
    for e in events:
        meta = e.get("metadata") or {}
        records = meta.get("provider_usage_audit") or []
        if meta.get("provider_usage") is not None:
            records = [{"provider_usage": meta["provider_usage"]}]
        available = [r["provider_usage"] for r in records if r.get("provider_usage")]
        raw_source = "provider_response"
        if (
            not available
            and (meta.get("usage_audit") or {}).get("kind") == "hermes_session_counters"
        ):
            available = meta["usage_audit"]["records"]
            raw_source = "hermes_session_counters"
        raw_output = sum(
            r.get("completion_tokens", r.get("output_tokens", 0)) or 0
            for r in available
        )
        raw_reasoning = sum(
            (r.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            for r in available
        )
        rows.append(
            {
                "usage_event_id": e["usage_event_id"],
                "stage": e["stage_key"],
                "source": e.get("usage_source"),
                "audit_counter_source": raw_source if available else None,
                "provider_records": len(available),
                "recorded_completion_tokens": e.get("completion_tokens"),
                "provider_completion_tokens": raw_output if available else None,
                "output_matches_provider": e.get("completion_tokens") == raw_output
                if available
                else None,
                "reasoning_tokens": raw_reasoning
                if available
                else e.get("reasoning_tokens"),
                "final_content_characters": meta.get("final_content_characters")
                or sum(r.get("final_content_characters") or 0 for r in records),
                "cost_kind": e.get("cost_kind"),
            }
        )
    return rows


def arm(item, args, mode):
    paper = item["paper_id"]
    folder = args.output_dir / paper / mode
    folder.mkdir(parents=True)
    bronze, source, miners, candidates, source_map = fixture_inputs(
        item, args.manifest.parent
    )
    os.environ["CLAIMS_ELIGIBILITY_OUTPUT_CONTRACT"] = mode
    collector = ModelUsageCollector(
        network="testnet",
        run_id=f"decision_replay_{mode}",
        batch_id=paper,
        checkpoint_sink=lambda events: write(folder / "usage_events.json", events),
        checkpoint_every=1,
    )
    cfg = replace(
        FileAgentWorkflowConfig.from_env(),
        root=folder / "workspaces",
        eligibility_output_contract=mode,
        eligibility_sample_verbose_paper=False,
        resume_existing_stages=False,
        fallback_to_legacy=False,
    )
    diagnostics = []
    artifact_paths = {}
    for i, miner in enumerate(miners):
        path = folder / "inputs" / f"{miner.miner_id}.json"
        write(path, miner.artifact)
        artifact_paths[miner.miner_id] = path
        _, parsed, structural = run_structural_checks(path)
        grounding = run_grounding_checks(parsed, source)
        diagnostics.append(
            DiagnosticBatchSubmission(
                submission_ref=f"S{i}",
                artifact=miner.artifact,
                source_payload=source,
                structural_findings=[x.model_dump(mode="json") for x in structural],
                grounding_findings=[x.model_dump(mode="json") for x in grounding],
            )
        )
    result_report = {"status": "running", "mode": mode}
    try:
        dc = replace(
            DiagnosticBatchConfig.from_env(),
            root=folder / "diagnostics",
            model=cfg.comparison_model,
            harness=cfg.harness,
            provider=cfg.provider,
        )
        execution = run_diagnostic_batch(
            config=dc,
            run_id="paired",
            paper_id=paper,
            submissions=diagnostics,
            usage_sink=collector.record if mode == "decision" else None,
        )
        for event in execution.usage_events if mode != "decision" else []:
            collector.record(
                {
                    **event,
                    "paper_id": paper,
                    "stage_key": "diagnostic_validation",
                    "stage_label": "Diagnostics",
                    "harness": "decision" if mode == "decision" else dc.harness,
                    "runtime": mode,
                    "provider": dc.provider,
                    "model": dc.model,
                }
            )
        if execution.error:
            raise RuntimeError(f"Diagnostics incomplete: {execution.error}")
        write(folder / "diagnostic_reports.json", execution.reports)
        findings = {}
        for i, miner in enumerate(miners):
            source_path = folder / "inputs" / "source.json"
            write(source_path, source)
            runner = AgentV1ValidatorRunner(AgentV1ValidatorConfig.from_env())
            report = runner.run(
                artifact_path=artifact_paths[miner.miner_id],
                source_payload_path=source_path,
                output_dir=folder / "diagnostic_reports" / miner.miner_id,
                precomputed_rigor=execution.reports[f"S{i}"],
                precomputed_rigor_manifest=precomputed_rigor_manifest(execution),
            )
            findings[miner.miner_id] = report.findings
        workflow = FileAgentSilverWorkflow(config=cfg, usage_sink=collector.record)
        result = run_paper_silver_pipeline(
            paper_id=paper,
            bronze_artifact=bronze,
            miner_artifacts=miners,
            silver_record_id=f"{mode}_{paper}",
            adjudication_passes=[],
            equivalence_shared_coverage=True,
            paper_context=source.get("paper") or bronze.get("paper") or {},
            source_context_by_span_id=source_map,
            eligibility_source_context_by_span_id=source_map,
            file_agent_workflow=workflow,
            max_eligible_claims_per_miner=10,
            max_adjudication_cases=1000,
            validation_findings_by_miner_id=findings,
        )
        write(
            folder / "silver_record.json", result.silver_record.model_dump(mode="json")
        )
        write(
            folder / "decisions.json",
            [
                d.model_dump(mode="json")
                for d in result.eligibility_adjudication_decisions
            ],
        )
        write(
            folder / "scores.json", [s.model_dump(mode="json") for s in result.scores]
        )
        controlled = [
            score_miner_against_silver(
                miner_id=s.miner_id,
                miner_candidates=s.candidates,
                silver_record=result.silver_record,
                normal_findings=[],
                equivalence_shared_coverage=True,
            )
            for s in result.miner_submissions
        ]
        write(
            folder / "scores_fixed_diagnostic_quality.json",
            [s.model_dump(mode="json") for s in controlled],
        )
        result_report.update(
            status="complete",
            silver_units=len(result.silver_record.silver_units),
            groups=len(result.equivalence_groups),
            retained=sorted(
                {
                    cid
                    for u in result.silver_record.silver_units
                    for cid in u.equivalent_candidate_ids
                }
            ),
            scores={s.miner_id: s.score for s in result.scores},
            ranks=ranks({s.miner_id: s.score for s in result.scores}),
        )
    except Exception:
        result_report.update(status="failed", traceback=traceback.format_exc())
    events = collector.snapshot()
    write(folder / "usage_events.json", events)
    write(folder / "usage_audit.json", audit_usage(events))
    result_report["usage"] = summarize(events)
    write(folder / "report.json", result_report)
    print(
        paper,
        mode,
        result_report["status"],
        result_report["usage"].get("all"),
        flush=True,
    )
    return result_report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument(
        "--modes",
        nargs="+",
        choices=["compact", "decision"],
        default=["compact", "decision"],
    )
    args = p.parse_args()
    args.manifest = args.manifest.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    items = read(args.manifest)[: args.limit]
    if not items or len({i["paper_id"] for i in items}) != len(items):
        raise ValueError("Distinct fixtures required")
    for item in items:
        fixture_inputs(item, args.manifest.parent)
    write(
        args.output_dir / "plan.json",
        {
            "items": items,
            "modes": args.modes,
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "reference_generation": "frozen, excluded",
            "verbose_sample": "disabled, separate audit budget",
            "scope": "fresh diagnostics, comparison, eligibility, canonicalization; no backend/chain writes",
        },
    )
    if args.validate_only:
        return
    if not args.env_file:
        p.error("--env-file required for model-backed replay")
    load_dotenv(args.env_file, override=True)
    os.environ["CLAIMS_BACKEND_URL"] = ""
    os.environ["CLAIMS_AUDIT_ONLY"] = "true"
    disable_response_cache()
    results = []
    for i, item in enumerate(items):
        order = args.modes if i % 2 == 0 else list(reversed(args.modes))
        paper = {"paper_id": item["paper_id"], "arms": {}}
        for mode in order:
            paper["arms"][mode] = arm(item, args, mode)
        results.append(paper)
        write(args.output_dir / "suite_status.json", results)
    return (
        0
        if all(a["status"] == "complete" for r in results for a in r["arms"].values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
