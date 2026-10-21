"""Run one saved batch through the real validator scoring entry point.

Only transport is replaced: saved miner responses and a read-only Bronze adapter.
No validator constructor/run loop, wallet, chain client, or backend is created.
The production argument parser, diagnostic batching, Silver orchestration, request
gate, verbose-paper selection, persistence-to-disk, and batch scoring are reused.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
from types import SimpleNamespace

from dotenv import load_dotenv

from scripts.benchmark_equivalence_routing import read, write, disable_response_cache
from scripts.replay_mainnet_decision import validate_batch
from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import ClaimsTask, PROTOCOL_VERSION, SCHEMA_VERSION
from neurons.validator import (
    ClaimsValidator,
    _require_bittensor,
    _paper_task_context,
    _source_context_map_from_payloads,
    _run_config_snapshot,
)
from validator.agent_v1.model_usage import ModelUsageCollector
from validator.agent_v1.eligibility_compact import sampled_verbose_paper


class SavedBronze:
    def __init__(self, items, root):
        self.items = {i["paper_id"]: i for i in items}
        self.root = root

    def get_or_create_bronze(self, *, request, reference_release_id):
        folder = self.root / self.items[request.paper_id]["fixture_dir"]
        assert (folder / "bronze.json").is_file()
        return SimpleNamespace(
            bronze_record_id=f"saved_{request.paper_id}",
            paper_id=request.paper_id,
            artifact_path=str(folder / "bronze.json"),
            source_payload_path=str(folder / "source.json"),
            metadata={"reused_saved_reference": True},
            reference_release_id=reference_release_id,
            reference_profile_id="saved-mainnet-reference",
            model_runtime_id="saved-reference",
            pipeline_version="saved-mainnet",
        )


def saved_batch(manifest):
    items, checks = validate_batch(manifest)
    if len(items) != 50 or not all(x["replayable"] for x in checks):
        raise ValueError("This replay requires one full 50-paper saved-input batch")
    root = manifest.parent
    historical = read(root / "historical/batch_score_result.json")
    orders = []
    for i in items:
        lineage = read(
            root / "historical/workspaces" / i["paper_id"] / "lineage/candidates.json"
        )
        orders.append(
            list(
                dict.fromkeys(
                    c["miner_id"] for c in lineage["candidates"] if c.get("miner_id")
                )
            )
        )
    order = max(orders, key=len)
    if len(order) != 10 or set(order) != {m["miner_id"] for m in historical["miners"]}:
        raise ValueError("Cannot reproduce original ten-miner response order")
    articles = {mid: [] for mid in order}
    papers = []
    contexts = {}
    for item, original_order in zip(items, orders):
        pid = item["paper_id"]
        folder = root / item["fixture_dir"]
        packet = read(
            root / "historical/workspaces" / pid / "executions/comparison/task.json"
        )
        contexts[pid] = packet["paper"]
        if [mid for mid in order if mid in original_order] != original_order:
            raise ValueError(f"{pid}: inconsistent historical miner order")
        payloads = [read(folder / "source.json")]
        for mid in order:
            if mid not in item["miner_ids"]:
                continue
            source = read(folder / f"source_{mid}.json")
            articles[mid].append(
                dict(
                    paper_id=pid,
                    status="completed",
                    agent_output=read(folder / f"miner_{mid}.json"),
                    source_payload=source,
                )
            )
            payloads.append(source)
        production_source = _source_context_map_from_payloads(payloads)
        referenced = {
            sid
            for c in read(
                root / "historical/workspaces" / pid / "lineage/candidates.json"
            )["candidates"]
            for sid in c.get("source_span_ids", [])
        }
        # The saved comparison packet omits unreferenced trailing source spans.
        if (
            any(
                production_source.get(sid) != text
                for sid, text in packet["source_spans"].items()
            )
            or (production_source.keys() - packet["source_spans"].keys()) & referenced
        ):
            raise ValueError(
                f"{pid}: production source precedence differs from saved task"
            )
        papers.append(
            dict(
                paper_id=pid,
                title=packet["paper"].get("title", ""),
                artifact={"paper": packet["paper"]},
            )
        )
    task = ClaimsTask.from_dict(
        dict(
            task_id="saved_validator_batch",
            batch_id=historical["batch_id"],
            network="testnet",
            papers=papers,
        )
    )
    for paper in task.paper_tasks():
        # Restore the original task context, including abstract/claims summary.
        object.__setattr__(paper, "metadata", contexts[paper.paper_id])
        assert _paper_task_context(paper) == contexts[paper.paper_id]
    responses = [
        ClaimExtractionSynapse(
            task_id=task.task_id,
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            miner_version="agent_v1",
            articles=articles[mid],
        )
        for mid in order
    ]
    return items, checks, historical, order, task, responses


def make_validator(args, task, historical, order):
    env = read(args.production_env)
    if any("API_KEY" in k for k in env):
        raise ValueError("Production settings snapshot must not contain credentials")
    if args.env_file:
        load_dotenv(args.env_file, override=True)
    os.environ.update({k: str(v) for k, v in env.items()})
    overrides = {
        "CLAIMS_BACKEND_URL": "",
        "CLAIMS_AUDIT_ONLY": "true",
        "CLAIMS_OUTPUT_DIR": str(args.output_dir / "outputs"),
        "CLAIMS_SILVER_FILE_WORKSPACE_ROOT": str(args.output_dir / "workspaces"),
        "CLAIMS_DIAGNOSTIC_FILE_WORKSPACE_ROOT": str(args.output_dir / "diagnostics"),
        "CLAIMS_REFERENCE_MINER_COMMAND": "",
        "CLAIMS_ELIGIBILITY_OUTPUT_CONTRACT": "decision",
        "CLAIMS_DECISION_REASONING_ENABLED": str(args.reasoning_enabled).lower(),
        "CLAIMS_DECISION_OPENROUTER_PROVIDER_ONLY": args.decision_provider,
        "CLAIMS_DECISION_BATCH_SIZE": "24",
        "CLAIMS_DECISION_MAX_TOKENS": "32768",
        "CLAIMS_DECISION_BLIND_MAX_TOKENS": "32768",
    }
    os.environ.update(overrides)
    disable_response_cache()
    task_path = args.output_dir / "task.json"
    write(task_path, asdict(task))
    v = ClaimsValidator.__new__(ClaimsValidator)
    v.Config, v.Dendrite, v.Subtensor, v.Wallet, v.bt_logging = _require_bittensor()
    saved_argv = sys.argv
    try:
        sys.argv = [
            "saved-validator-replay",
            "--netuid",
            "530",
            "--claims.dry-run",
            "--claims.task-manifest",
            str(task_path),
            "--claims.output-dir",
            str(args.output_dir / "outputs"),
            "--logging.logging_dir",
            str(args.output_dir / "logs"),
        ]
        v.config = v._get_config()
    finally:
        sys.argv = saved_argv
    v.bt_logging = logging.getLogger("saved-validator-replay")
    v.backend_client = None
    # Read-only identity for audit metadata; no wallet keys or chain client.
    v.wallet = SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address="saved-replay-audit-only")
    )
    v.target_neurons = [
        SimpleNamespace(
            uid=int(mid.removeprefix("uid_")), hotkey="", coldkey="", axon_info=None
        )
        for mid in order
    ]
    v._active_run_timing = None
    v._active_silver_batch_outcome = {}
    v._active_miner_selection = dict(
        assignments=[
            dict(
                uid=int(m["miner_id"].removeprefix("uid_")),
                selection_lane=m.get("selection_lane", ""),
            )
            for m in historical["miners"]
        ],
        selection_policy=historical.get("selection_policy", {}),
    )
    v._model_usage_checkpoint_lock = threading.Lock()
    v._adjudication_progress_lock = threading.Lock()
    v._adjudication_progress_seen_cases = set()
    v._adjudication_progress_seen_votes = set()
    collector = ModelUsageCollector(
        network="testnet_replay",
        run_id=args.output_dir.name,
        batch_id=task.batch_id,
        checkpoint_sink=lambda es: write(args.output_dir / "usage_events.json", es),
        checkpoint_every=1,
    )
    v._active_model_usage = collector
    v._build_reference_miner_client = lambda: SavedBronze(
        read(args.manifest), args.manifest.parent
    )
    # Build exactly as production does and verify shared-gate wiring before calls.
    passes, _ = v._build_silver_adjudication_passes()
    wf = v._build_silver_file_agent_workflow(passes)
    assert (
        wf is not None
        and wf.request_gate is passes[0].request_gate
        and wf.request_gate is not None
    )
    assert (
        wf.config.decision_reasoning_enabled == args.reasoning_enabled
        and wf.config.decision_max_tokens == 32768
    )
    return (
        v,
        collector,
        dict(
            overrides=overrides,
            production_env_sha256=hashlib.sha256(
                args.production_env.read_bytes()
            ).hexdigest(),
            resolved=_run_config_snapshot(v.config),
            workflow=wf.config.__dict__,
            shared_request_gate=True,
            verbose_paper_id=sampled_verbose_paper(
                task.batch_id, [p.paper_id for p in task.paper_tasks()]
            ),
        ),
    )


def reconcile_diagnostics(v, collector, output):
    """Retain partial diagnostic calls even when a paper operation raised."""
    ids = {
        e.get("metadata", {}).get("provider_response_id") for e in collector.snapshot()
    }
    added = 0
    for path in (output / "diagnostics").rglob("usage_events.json"):
        for event in read(path):
            pid = event.get("metadata", {}).get("provider_response_id")
            if pid and pid not in ids:
                collector.record({**event, "stage_key": "diagnostic_validation"})
                ids.add(pid)
                added += 1
    write(
        output / "diagnostic_usage_reconciliation.json",
        dict(recovered_partial_events=added),
    )
    v._upload_active_model_usage_checkpoint()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--production-env", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument(
        "--reasoning-enabled", action=argparse.BooleanOptionalAction, default=True
    )
    p.add_argument(
        "--decision-provider",
        default="",
        help="Pin decision requests to an OpenRouter endpoint; disables provider fallbacks",
    )
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--probe-only", action="store_true")
    args = p.parse_args()
    args.manifest = args.manifest.resolve()
    args.production_env = args.production_env.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    items, checks, historical, order, task, responses = saved_batch(args.manifest)
    write(args.output_dir / "input_validation.json", checks)
    v, collector, settings = make_validator(args, task, historical, order)
    write(
        args.output_dir / "plan.json",
        dict(
            commit=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            batch_id=task.batch_id,
            original_run_id=args.manifest.parent.name,
            paper_count=len(items),
            miner_order=order,
            settings=settings,
            scope="One frozen batch through ClaimsValidator._score_responses; no chain/backend; reference reused; no compact rerun; no checkpoint reuse.",
        ),
    )
    if args.preflight_only:
        print(
            "Validated one 50-paper batch; production parser, source order and request gate match."
        )
        return 0
    if args.probe_only:
        from validator.agent_v1.diagnostic_batch import (
            DiagnosticBatchSubmission,
            DiagnosticBatchConfig,
            run_diagnostic_batch,
        )

        article = responses[0].articles[0]
        cfg = replace(
            DiagnosticBatchConfig.from_env(), root=args.output_dir / "diagnostics"
        )
        result = run_diagnostic_batch(
            config=cfg,
            run_id=args.output_dir.name,
            paper_id=article["paper_id"],
            submissions=[
                DiagnosticBatchSubmission(
                    submission_ref="S0001",
                    artifact=article["agent_output"],
                    source_payload=article["source_payload"],
                    structural_findings=[],
                    grounding_findings=[],
                )
            ],
            usage_sink=collector.record,
        )
        write(
            args.output_dir / "probe_result.json",
            dict(
                error=result.error,
                reports=len(result.reports),
                events=collector.snapshot(),
            ),
        )
        if result.error:
            raise RuntimeError(result.error)
        return 0
    status = dict(status="running", started_at=datetime.now(timezone.utc).isoformat())
    write(args.output_dir / "status.json", status)
    try:
        scores = v._score_responses(responses, task=task, run_id=args.output_dir.name)
        write(args.output_dir / "scores_by_uid.json", scores)
        outcome = v._active_silver_batch_outcome
        status.update(
            status="complete"
            if outcome.get("scored_paper_count") == 50
            else "completed_with_failures",
            outcome=outcome,
        )
    except Exception:
        status.update(status="failed", traceback=traceback.format_exc())
        raise
    finally:
        reconcile_diagnostics(v, collector, args.output_dir)
        status["ended_at"] = datetime.now(timezone.utc).isoformat()
        write(args.output_dir / "status.json", status)
        write(args.output_dir / "usage_events.json", collector.snapshot())


if __name__ == "__main__":
    main()
