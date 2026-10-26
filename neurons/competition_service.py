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
from typing import Any

from competition.intake_lock import CompetitionBackend
from competition.service_loops import run_controller_cycle, run_worker_cycle


def _factory(module_name: str) -> Any:
    if (not module_name.startswith("competition.")
        or any(not part.isidentifier() for part in module_name.split("."))):
        raise ValueError("adapter factory must be in the trusted competition package")
    return importlib.import_module(module_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("controller", "worker"), required=True)
    parser.add_argument("--network", choices=("mainnet", "testnet"), required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--factory-module", required=True)
    parser.add_argument("--worker-id")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 600:
        parser.error("--poll-seconds must be between 1 and 600")
    if args.role == "worker" and not args.worker_id:
        parser.error("worker mode needs --worker-id")
    if args.role == "controller" and args.worker_id:
        parser.error("controller mode cannot have a worker ID")
    token = os.environ.get("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "")
    if not token:
        parser.error("CLAIMS_COMPETITION_CONTROLLER_TOKEN is required")
    try:
        backend = CompetitionBackend(base_url=args.backend_url, controller_token=token)
        factory = _factory(args.factory_module)
        callback_name = ("create_controller_inputs" if args.role == "controller"
                         else "create_worker_coordinator")
        callback = getattr(factory, callback_name)
        if not callable(callback):
            raise ValueError("trusted adapter factory has no callable entrypoint")
    except (ImportError, AttributeError, ValueError) as exc:
        print(json.dumps({"status": "startup_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1
    while True:
        try:
            if args.role == "controller":
                results = run_controller_cycle(
                    backend=backend, network=args.network,
                    inputs_for_round=lambda row: callback(row, backend),
                )
            else:
                results = run_worker_cycle(
                    backend=backend, network=args.network,
                    coordinator_for_round=lambda row: callback(row, backend, args.worker_id),
                )
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
            time.sleep(1 if args.role == "worker" and any(
                row["status"] == "job_processed" for row in results
            ) else args.poll_seconds)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
