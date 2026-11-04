"""Poll durable weekly rounds from a trusted controller or worker process.

The factory module must be part of the deployed Claims checkout. It supplies
the privileged round adapters; miner packages are never imported here.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from competition.intake_lock import CompetitionBackend
from competition.service_loops import run_controller_cycle, run_worker_cycle, run_bronze_cycle, run_intake_cycle


def _factory(module_name: str) -> Any:
    if (not module_name.startswith("competition.")
        or any(not part.isidentifier() for part in module_name.split("."))):
        raise ValueError("adapter factory must be in the trusted competition package")
    return importlib.import_module(module_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("controller", "bronze", "worker", "handoff", "production"), required=True)
    parser.add_argument("--network", choices=("mainnet", "testnet"), required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--factory-module", required=True)
    parser.add_argument("--worker-id")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--recurrence-config", type=Path,
                        help="Primary controller recurrence profile installed by infra")
    parser.add_argument("--duplicate-review-policy", type=Path,
                        help="Trusted automatic package preflight and review policy installed by infra")
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 600:
        parser.error("--poll-seconds must be between 1 and 600")
    if args.role in {"worker", "production"} and not args.worker_id:
        parser.error("worker and production modes need --worker-id")
    if args.role not in {"worker", "production"} and args.worker_id:
        parser.error("only worker and production modes accept a worker ID")
    if args.recurrence_config is not None and args.role != "controller":
        parser.error("only the primary controller can create recurring rounds")
    if args.duplicate_review_policy is not None and args.role != "controller":
        parser.error("only the primary controller can run duplicate preflight")
    token = os.environ.get("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "")
    if not token:
        parser.error("CLAIMS_COMPETITION_CONTROLLER_TOKEN is required")
    try:
        backend = CompetitionBackend(base_url=args.backend_url, controller_token=token)
        factory = _factory(args.factory_module)
        callback_name = {"controller": "create_controller_inputs", "bronze": "create_bronze_inputs",
                         "worker": "create_worker_coordinator",
                         "handoff": "create_handoff_service",
                         "production": "create_production_worker"}[args.role]
        callback = getattr(factory, callback_name)
        if not callable(callback):
            raise ValueError("trusted adapter factory has no callable entrypoint")
        handoff = callback(backend, args.backend_url, args.network) if args.role == "handoff" else None
        production = callback(
            backend, network=args.network, worker_id=args.worker_id,
        ) if args.role == "production" else None
        recurrence = None
        independent_intake = False
        if args.recurrence_config is not None:
            os.environ["CLAIMS_COMPETITION_RECURRENCE_CONFIG"] = str(args.recurrence_config)
            from competition.pilot_recurrence_factory import create_round_scheduler
            recurrence = create_round_scheduler(backend, network=args.network,
                                                profile_path=args.recurrence_config)
            from competition.recurrence import read_json
            independent_intake = read_json(args.recurrence_config).get("independent_intake", False)
            if independent_intake and not all(callable(getattr(factory, name, None))
                    for name in ("create_intake_inputs", "prepare_evaluation")):
                raise ValueError("independent intake needs trusted intake and evaluation adapters")
        if args.duplicate_review_policy is not None:
            from neurons.competition_duplicate_automation import read_policy
            read_policy(args.duplicate_review_policy)
            if not independent_intake:
                raise ValueError("automatic preflight requires the independent intake loop")
    except (ImportError, AttributeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "startup_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1
    evaluation_pool = ThreadPoolExecutor(max_workers=1) if independent_intake and not args.once else None
    evaluation_future = None
    while True:
        try:
            if args.role in {"controller", "bronze"}:
                recurring_results = []
                intake_results = (run_intake_cycle(backend=backend, network=args.network,
                    inputs_for_round=lambda row: factory.create_intake_inputs(row, backend))
                    if independent_intake else [])
                if recurrence is not None:
                    try:
                        result = recurrence.tick()
                        if result is not None:
                            recurring_results.append(result)
                    except Exception as exc:
                        recurring_results.append({"status": "error", "component": "recurrence",
                                                  "error_type": type(exc).__name__})
                if independent_intake:
                    results = intake_results + recurring_results
                    from neurons.competition_duplicate_automation import prepare_bound_round
                    options = dict(backend=backend, network=args.network,
                        inputs_for_round=lambda row: callback(row, backend),
                        separate_intake=True, evaluation_preparer=lambda row, client: prepare_bound_round(
                            row, client, factory, args.duplicate_review_policy))
                    if args.once:
                        results += run_controller_cycle(**options)
                    else:
                        if evaluation_future is not None and evaluation_future.done():
                            completed = evaluation_future
                            evaluation_future = None
                            results += completed.result()
                        if evaluation_future is None:
                            evaluation_future = evaluation_pool.submit(run_controller_cycle, **options)
                else:
                    cycle = run_controller_cycle if args.role == "controller" else run_bronze_cycle
                    results = recurring_results + cycle(
                        backend=backend, network=args.network,
                        inputs_for_round=lambda row: callback(row, backend),
                    )
            elif args.role == "worker":
                results = run_worker_cycle(
                    backend=backend, network=args.network,
                    coordinator_for_round=lambda row: callback(row, backend, args.worker_id),
                )
            elif args.role == "handoff":
                results = handoff.run_once()
            else:
                production_result = production.run_once()
                results = ([{"status": "job_processed",
                             "job_status": production_result["status"]}]
                           if production_result is not None else [])
            for result in results:
                print(json.dumps(result, sort_keys=True), flush=True)
            failed = any(row["status"] == "error" for row in results)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(json.dumps({"status": "poll_error", "error_type": type(exc).__name__}),
                  file=sys.stderr, flush=True)
            failed = True
            results = []
        if args.once:
            return 1 if failed else 0
        try:
            time.sleep(1 if args.role in {"worker", "production"} and any(
                row["status"] == "job_processed" for row in results
            ) else args.poll_seconds)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
