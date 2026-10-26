"""Poll one independent weekly replay without a primary controller token."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from competition.replay_round_registry import ReplayRoundRegistry


def _factory(module_name: str) -> Any:
    if (not module_name.startswith("competition.")
        or any(not part.isidentifier() for part in module_name.split("."))):
        raise ValueError("replay adapter must be in the trusted competition package")
    return importlib.import_module(module_name)


def _safe_status(result: dict[str, Any]) -> str:
    status = result.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,80}", status):
        raise ValueError("replay step returned an invalid status")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("controller", "worker"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--round-id")
    mode.add_argument("--auto-rounds", action="store_true")
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--db-dir", type=Path)
    parser.add_argument("--factory-module", default="competition.replay_pilot_factory")
    parser.add_argument("--worker-id")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.round_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", args.round_id):
        parser.error("--round-id is invalid")
    if args.auto_rounds != bool(args.db_dir) or bool(args.round_id) != bool(args.db_path):
        parser.error("one round needs --db-path; --auto-rounds needs --db-dir")
    if not 1 <= args.poll_seconds <= 600:
        parser.error("--poll-seconds must be between 1 and 600")
    if (args.role == "worker") != bool(args.worker_id):
        parser.error("worker mode needs --worker-id; controller mode cannot use it")
    try:
        factory = _factory(args.factory_module)
        if args.auto_rounds:
            rounds_dir = Path(os.environ["CLAIMS_COMPETITION_REPLAY_ROUNDS_DIR"])
            registry = ReplayRoundRegistry(
                factory=factory, role=args.role, backend_url=args.backend_url,
                rounds_dir=rounds_dir, db_dir=args.db_dir, worker_id=args.worker_id,
            )
            runner = None
        else:
            registry = None
            runner = (factory.create_replay_controller(
                args.round_id, args.backend_url, args.db_path,
            ) if args.role == "controller" else factory.create_replay_worker(
                args.round_id, args.backend_url, args.db_path, args.worker_id,
            ))
    except (ImportError, AttributeError, KeyError, OSError, ValueError) as exc:
        print(json.dumps({"status": "startup_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1
    while True:
        try:
            if registry is not None:
                results = registry.run_once()
                for row in results:
                    print(json.dumps(row, sort_keys=True),
                          file=sys.stderr if row["status"] == "error" else sys.stdout,
                          flush=True)
                failed = any(row["status"] == "error" for row in results)
                status = "busy" if any(row["status"] not in {"idle", "error"}
                                       for row in results) else "idle"
            else:
                result = runner.run_once()
                status = "idle" if result is None else _safe_status(result)
                print(json.dumps({"round_id": args.round_id, "role": args.role,
                                  "status": status}, sort_keys=True), flush=True)
                failed = False
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(json.dumps({"round_id": args.round_id, "role": args.role,
                              "status": "error", "error_type": type(exc).__name__},
                             sort_keys=True), file=sys.stderr, flush=True)
            failed = True
            status = "error"
        if args.once:
            return 1 if failed else 0
        try:
            time.sleep(1 if args.role == "worker" and status not in {"idle", "error"}
                       else args.poll_seconds)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
