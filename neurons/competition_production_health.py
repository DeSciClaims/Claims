"""Check active production health with a read-only backend credential."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from competition.intake_lock import CompetitionBackend
from competition.production_monitor import check_production_health
from competition.production_reader import ProductionReleaseReader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("mainnet", "testnet"), required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--max-queued-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    token = os.environ.get("CLAIMS_PRODUCTION_RESULTS_TOKEN", "")
    if not token:
        parser.error("CLAIMS_PRODUCTION_RESULTS_TOKEN is required")
    try:
        backend = CompetitionBackend(base_url=args.backend_url, controller_token=token)
        report = check_production_health(
            network=args.network, reader=ProductionReleaseReader(args.production_root),
            backend=backend, max_queued_seconds=args.max_queued_seconds,
        )
        print(json.dumps(report, sort_keys=True), flush=True)
        return 2 if report["status"] == "alert" else 0
    except Exception as exc:
        print(json.dumps({"status": "health_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
