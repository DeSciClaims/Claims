"""Compare a frozen competition package with saved historical winner outputs.

Uses the production sandbox and Silver workflow, with local benchmark receipts.
No chain weights, competition rounds, consensus cases or backend rows are written.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
from types import SimpleNamespace

from competition.controller_inputs import build_provisional_evaluation_policy
from competition.judge_usage import JudgeUsageCapture
from competition.pair_evaluator import evaluate_weekly_paper
from competition.pilot_worker_factory import _bifrost_admin_client, _worker_execution
from competition.production_reader import ActiveProductionPackage
from competition.production_runner import run_production_paper
from competition.publication import canonical_sha256
from competition.silver_runtime import SILVER_ENV_DEFAULTS, silver_runtime_config
from competition.ssh_worker import RestrictedSshWorkerTransport
from competition.validator_workflow import build_validator_workflow


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False,
                                    allow_nan=False, default=_json_default) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def checked_file(root, relative, digest):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("benchmark input is outside its directory")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"benchmark input hash differs: {relative}")
    return path


def paper_key(paper_id):
    return hashlib.sha256(paper_id.encode()).hexdigest()[:24]


def evaluate_pair(data, incumbent_artifact, *, panels, tiebreak, record_id,
                  workflow=None, judge_id="J1"):
    """Compare two outputs, reusing the historical output as the reference.

    The normal workflow requires a reference side for comparison. The same
    historical extraction is also the baseline submission for pairwise credit;
    this introduces no third extraction or independent reference claims.
    """
    return evaluate_weekly_paper(
        paper_id=data["paper_id"],
        bronze_artifact=data["historical_artifact"],
        artifacts_by_hotkey={"pipeline_a": data["historical_artifact"],
                             "pipeline_b": incumbent_artifact},
        baseline_hotkey="pipeline_a", challenger_hotkeys=["pipeline_b"],
        silver_record_id=record_id, adjudication_passes_by_judge=panels,
        tiebreak_pass=tiebreak, judge_id=judge_id, stage="final",
        source_context_by_span_id=data["source_context"],
        file_agent_workflow=workflow,
    )


class LocalResults:
    def __init__(self, path):
        self.path = path

    def complete_production_job(self, job_id, **kwargs):
        # The complete usage/output receipt is durable before acknowledging it.
        write(self.path, {"job_id": job_id, **kwargs["result"],
                          "finished_at": datetime.now(UTC).isoformat()})


class LocalLogs:
    def __init__(self, path):
        self.path = path

    def put(self, key, content):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_bytes(content)
        self.path.chmod(0o600)


def prepare(config_path):
    root = config_path.resolve().parent
    config = read(config_path)
    if config.get("schema_version") != "claims_historical_extraction_benchmark_v1":
        raise ValueError("unknown historical extraction benchmark schema")
    if type(config["paper_concurrency"]) is not int or not 1 <= config["paper_concurrency"] <= 8:
        raise ValueError("benchmark concurrency must be between one and eight")
    papers = config["papers"]
    if not papers or len({p["paper_id"] for p in papers}) != len(papers):
        raise ValueError("benchmark papers must be nonempty and distinct")
    package = config["package"]
    package_path = checked_file(root, package["path"], package["sha256"])
    settings = config["silver_settings"]
    if set(settings) != set(SILVER_ENV_DEFAULTS):
        raise ValueError("benchmark must specify the complete Silver configuration")
    for name, value in settings.items():
        os.environ[name] = str(value)
    inputs = {}
    for row in papers:
        data = read(checked_file(root, row["input_path"], row["input_sha256"]))
        checked_file(root, row["pdf_path"], row["pdf_sha256"])
        if (data["paper_id"] != row["paper_id"]
            or data["historical_artifact"]["paper"]["paper_id"] != row["paper_id"]
            or not data["source_context"] or data["pdf_sha256"] != row["pdf_sha256"]):
            raise ValueError("historical input is inconsistent or lacks source text")
        inputs[row["paper_id"]] = data
    runtime = {"config": config, "silver_runtime": silver_runtime_config(),
               "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    # Credential presence is an operational check rather than a benchmark rule.
    runtime["silver_runtime"].pop("embedding_credential_present", None)
    seal = root / "frozen.json"
    fingerprint = canonical_sha256(runtime)
    if seal.exists() and read(seal)["sha256"] != fingerprint:
        raise ValueError("benchmark configuration or runtime changed; use a new directory")
    if not seal.exists():
        write(seal, {"sha256": fingerprint, "runtime": runtime,
                     "created_at": datetime.now(UTC).isoformat()})
    return root, config, package_path.read_bytes(), inputs, fingerprint


def extract(root, config, package, inputs):
    """Use the existing worker boundary; never install miner code on this host."""
    worker = config["worker_policy"]
    gateway = worker["inference_base_url"]
    from urllib.parse import urlsplit
    transport = RestrictedSshWorkerTransport(
        identity_file=Path(os.environ["CLAIMS_WORKER_SSH_IDENTITY"]),
        known_hosts_file=Path(os.environ["CLAIMS_WORKER_SSH_KNOWN_HOSTS"]),
        inference_base_url=gateway,
        gateway_ip=os.environ["CLAIMS_WORKER_GATEWAY_IP"],
        gateway_port=urlsplit(gateway).port or 443,
    )
    execution, keys = _worker_execution(transport), _bifrost_admin_client()
    now = datetime.now(UTC)
    active = ActiveProductionPackage(
        round_id=config["benchmark_id"], receipt_sha256=config["package"]["receipt_sha256"],
        hotkey=config["package"]["hotkey"], package_sha256=config["package"]["sha256"],
        activate_at=now, latest_activation_at=now, rolled_back_from=None,
        package=package, manifest=config["package"]["manifest"],
    )
    for row in config["papers"]:
        token = paper_key(row["paper_id"])
        result_path = root / "executions" / f"{token}.json"
        started_path = root / "executions" / f"{token}.started.json"
        if result_path.exists():
            continue
        if started_path.exists():
            raise RuntimeError("unacknowledged execution needs usage reconciliation: " + row["paper_id"])
        if execution.capacity()["available_slots"] < 1:
            raise RuntimeError("shared worker is busy; retry later without changing this benchmark")
        data = inputs[row["paper_id"]]
        job_id = hashlib.sha256((config["benchmark_id"] + "\0" + row["paper_id"]).encode()).hexdigest()
        lease = {"job_id": job_id, "paper_id": row["paper_id"],
                 "lease_token": job_id, "source_sha256": row["pdf_sha256"],
                 "source_url": data["pdf_source_url"], "paper": data["paper"],
                 "attempts": 1, "receipt_sha256": active.receipt_sha256,
                 "package_sha256": active.package_sha256}
        write(started_path, {"job_id": job_id, "started_at": datetime.now(UTC).isoformat()})
        run_production_paper(
            active, lease, network="testnet", worker_id=config["benchmark_id"],
            fetch_pdf=lambda url, sha: checked_file(root, row["pdf_path"], sha).read_bytes(),
            keys=keys, execution=execution,
            logs=LocalLogs(root / "logs" / f"{token}.txt"), results=LocalResults(result_path),
            inference_base_url=gateway, allowed_models=tuple(worker["allowed_models"]),
            token_limit=worker["published_token_ceiling"],
            dollar_limit=float(worker["dollar_limit_usd"]),
            max_inference_output_tokens=worker["max_inference_output_tokens"],
            key_lifetime_seconds=min(3600, worker["lease_seconds"]),
        )
        print("Extraction saved:", row["paper_id"], flush=True)


def score(root, config, inputs, fingerprint):
    capture = JudgeUsageCapture()
    policy = build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(round_id=config["benchmark_id"],
            source_context_for_paper=lambda pid: inputs[pid]["source_context"]),
        decision_api_key="unused-final-only", weekly_budget_usd=1,
        worker_policy=config["worker_policy"], judge_specs=config["judge_specs"],
        workflow_specs=config["workflow_specs"], judge_usage_sink=capture.record,
    )

    def one(row):
        token = paper_key(row["paper_id"])
        path = root / "scores" / f"{token}.json"
        if path.exists():
            return read(path)
        execution = read(root / "executions" / f"{token}.json")
        artifact = execution.get("artifact") if execution["status"] == "completed" else None
        judge_id = "J1" if int(token, 16) % 2 == 0 else "J2"
        workflow = build_validator_workflow(policy, stage="final", judge_id=judge_id,
                                            decision_max_workers=1)
        with capture.collect(row["paper_id"]) as events:
            result = evaluate_pair(
                inputs[row["paper_id"]], artifact,
                panels=policy.adjudication_passes_by_judge,
                tiebreak=policy.tiebreak_pass, record_id="historical_" + token,
                workflow=workflow, judge_id=judge_id,
            )
        output = {"paper_id": row["paper_id"], "historical_run_id": row["historical_run_id"],
                  "execution_status": execution["status"],
                  "paired_score": result.pair_rows[0], "evaluation": asdict(result),
                  "usage_events": events, "benchmark_sha256": fingerprint,
                  "finished_at": datetime.now(UTC).isoformat()}
        write(path, output)
        print("Score saved:", row["paper_id"], flush=True)
        return output

    # A saved partial run resumes paper checkpoints and existing workflow stages.
    with ThreadPoolExecutor(max_workers=config["paper_concurrency"]) as pool:
        outputs = list(pool.map(one, config["papers"]))
    gains = [v["paired_score"]["challenger_score"] - v["paired_score"]["baseline_score"] for v in outputs]
    summary = {"benchmark_sha256": fingerprint, "papers": len(outputs),
               "historical_mean": statistics.mean(v["paired_score"]["baseline_score"] for v in outputs),
               "incumbent_mean": statistics.mean(v["paired_score"]["challenger_score"] for v in outputs),
               "mean_gain": statistics.mean(gains),
               "standard_error": statistics.stdev(gains) / len(gains) ** 0.5 if len(gains) > 1 else None,
               "wins": sum(g > 0 for g in gains), "ties": sum(g == 0 for g in gains),
               "losses": sum(g < 0 for g in gains),
               "extraction_failures": sum(v["execution_status"] != "completed" for v in outputs),
               "finished_at": datetime.now(UTC).isoformat()}
    write(root / "summary.json", summary)
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--phase", choices=("preflight", "extract", "score"), default="preflight")
    args = parser.parse_args()
    import dspy
    dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)
    root, config, package, inputs, fingerprint = prepare(args.config)
    os.environ["CLAIMS_COMPETITION_ROUNDS_DIR"] = str(root / "workspaces")
    if args.phase == "extract":
        extract(root, config, package, inputs)
    elif args.phase == "score":
        score(root, config, inputs, fingerprint)
    else:
        print(json.dumps({"status": "inputs_ready", "papers": len(inputs),
                          "package_sha256": config["package"]["sha256"],
                          "benchmark_sha256": fingerprint}, indent=2))


if __name__ == "__main__":
    main()
