"""Freeze reviewed duplicate links after intake lock and before sample reveal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from competition.code_audit_setup import _read_json
from competition.duplicate_review_setup import prepare_duplicate_review
from competition.intake_lock import CompetitionBackend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--rounds-dir", required=True, type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--links-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        token = os.environ["CLAIMS_COMPETITION_CONTROLLER_TOKEN"]
        backend = CompetitionBackend(base_url=args.backend_url, controller_token=token)
        links = _read_json(args.links_file).get("links")
        result = prepare_duplicate_review(
            args.round_id, backend=backend, directory=args.rounds_dir, links=links,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "review_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
