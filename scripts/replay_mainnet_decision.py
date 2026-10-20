"""Decision-only replay of a saved full mainnet batch; no production clients."""

from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import traceback
from dotenv import load_dotenv
from scripts.benchmark_equivalence_routing import read, write, disable_response_cache
from scripts.compare_decision_mode import arm
from scripts.replay_mainnet_batch import inputs


def frozen_inputs(item, root):
    """Use the original comparison packet to preserve duplicate-span precedence."""
    bronze, source, miners, candidates, merged, eligibility = inputs(item, root)
    task = read(
        root
        / "historical/workspaces"
        / item["paper_id"]
        / "executions/comparison/task.json"
    )
    historical_source = task["source_spans"]
    if not isinstance(historical_source, dict):
        raise ValueError("Expected the original source map")
    return (
        bronze,
        source,
        miners,
        candidates,
        historical_source,
        eligibility,
        task["paper"],
    )


def validate_batch(manifest):
    items = read(manifest)
    batch = read(manifest.parent / "historical/batch_score_result.json")
    if {i["paper_id"] for i in items} != set(batch["expected_paper_ids"]):
        raise ValueError("Manifest must include the entire assigned batch")
    checks = []
    from validator.agent_v1.orchestrator import _select_assessed_candidates
    from validator.agent_v1.record_projection import project_agent_artifact

    for item in items:
        if not (manifest.parent / item["fixture_dir"] / "bronze.json").exists():
            if item["paper_id"] not in batch["validator_failed_paper_ids"]:
                raise ValueError("Missing reference for a historically scored paper")
            checks.append(
                dict(
                    paper_id=item["paper_id"],
                    replayable=False,
                    reason="Historical reference generation failed; no saved reference artifact",
                )
            )
            continue
        bronze, source, miners, candidates, merged, eligibility, paper = frozen_inputs(
            item, manifest.parent
        )
        selected = project_agent_artifact(bronze, origin="bronze")
        for miner in miners:
            selected += _select_assessed_candidates(
                project_agent_artifact(
                    miner.artifact, origin="miner", miner_id=miner.miner_id
                ),
                [],
                max_claims=10,
                filter_by_assessment=False,
            )
        lineage = (
            manifest.parent
            / "historical/workspaces"
            / item["paper_id"]
            / "lineage/candidates.json"
        )
        match = None
        if lineage.exists():
            before = {c["candidate_id"]: c for c in read(lineage)["candidates"]}
            current = {c.candidate_id: c for c in selected}
            if set(before) != set(current):
                raise ValueError(
                    f"{item['paper_id']}: historical candidate ID mismatch"
                )
            for cid, candidate in current.items():
                if candidate.statement != before[cid]["statement"]:
                    raise ValueError(
                        f"{item['paper_id']}: historical statement mismatch: {cid}"
                    )
            match = True
        elif item["paper_id"] not in batch["validator_failed_paper_ids"]:
            raise ValueError(
                "Missing candidate snapshot for a historically scored paper"
            )
        for mid in item["miner_ids"]:
            read(manifest.parent / item["fixture_dir"] / f"report_{mid}.json")
        checks.append(
            dict(
                paper_id=item["paper_id"],
                candidates=len(candidates),
                miners=len(miners),
                historical_candidate_match=match,
                replayable=True,
                source_precedence="exact_historical_comparison_packet",
                source_spans=len(merged),
                eligibility_source_spans=len(eligibility),
                unresolved_candidate_spans=sorted(
                    {sid for c in candidates for sid in c.source_span_ids}
                    - merged.keys()
                ),
            )
        )
    return items, checks


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--workers", type=int, choices=range(1, 41), default=3)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Recover identical saved calls and retain their original usage",
    )
    args = p.parse_args()
    args.manifest = args.manifest.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.output_dir.exists() and not args.resume:
        p.error("Use a new output directory or explicit --resume")
    items, checks = validate_batch(args.manifest)
    args.output_dir.mkdir(parents=True, exist_ok=args.resume)
    write(args.output_dir / "input_validation.json", checks)
    plan = dict(
        items=items,
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        historical_run_id=items[0]["source_run_id"],
        mode="decision",
        workers=args.workers,
        manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        scope="Full assigned batch; fresh decision diagnostics/comparison/eligibility/canonicalization. Frozen extraction/reference. No compact rerun, verbose sample, backend or chain writes.",
    )
    if args.resume:
        previous = read(args.output_dir / "plan.json")
        if (
            previous["manifest_sha256"] != plan["manifest_sha256"]
            or previous["mode"] != "decision"
        ):
            raise ValueError(
                "Recovery requires the same frozen inputs and decision mode"
            )
        plan["recovery_history"] = previous.get("recovery_history", []) + [
            dict(
                commit=previous["commit"],
                recovered_at=datetime.now(timezone.utc).isoformat(),
            )
        ]
    write(args.output_dir / "plan.json", plan)
    if args.validate_only:
        return 0
    if not args.env_file:
        p.error("--env-file required")
    load_dotenv(args.env_file, override=True)
    os.environ["CLAIMS_BACKEND_URL"] = ""
    os.environ["CLAIMS_AUDIT_ONLY"] = "true"
    os.environ["CLAIMS_ELIGIBILITY_OUTPUT_CONTRACT"] = "decision"
    os.environ["CLAIMS_ELIGIBILITY_SAMPLE_VERBOSE_PAPER"] = "false"
    disable_response_cache()
    args.mainnet_inputs = True
    if args.resume:
        from scripts.decision_replay_checkpoint import install

        install()
    status = dict(
        status="running", started_at=datetime.now(timezone.utc).isoformat(), papers=[]
    )
    write(args.output_dir / "suite_status.json", status)
    replayable = {c["paper_id"] for c in checks if c.get("replayable")}
    status["papers"] = [
        dict(paper_id=c["paper_id"], status="unreplayable", reason=c["reason"])
        for c in checks
        if not c.get("replayable")
    ]
    completed = set()
    if args.resume:
        for item in items:
            path = args.output_dir / item["paper_id"] / "decision/report.json"
            if path.exists() and read(path).get("status") == "complete":
                completed.add(item["paper_id"])
                status["papers"].append(dict(paper_id=item["paper_id"], **read(path)))
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(arm, item, args, "decision"): item
            for item in items
            if item["paper_id"] in replayable and item["paper_id"] not in completed
        }
        for future in as_completed(futures):
            try:
                report = future.result()
            except Exception:
                report = dict(status="failed", traceback=traceback.format_exc())
            status["papers"].append(
                dict(paper_id=futures[future]["paper_id"], **report)
            )
            write(args.output_dir / "suite_status.json", status)
    status["status"] = (
        "complete"
        if all(r["status"] in {"complete", "unreplayable"} for r in status["papers"])
        else "failed"
    )
    status["ended_at"] = datetime.now(timezone.utc).isoformat()
    write(args.output_dir / "suite_status.json", status)
    return 0 if status["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
