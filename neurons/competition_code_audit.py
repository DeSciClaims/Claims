"""Produce a final code-audit draft, then bind reviewed decisions to it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from competition.code_audit_setup import _read_json, prepare_code_audit
from competition.hippius import HippiusPrivateStorage, hippius_s3_client
from competition.intake_lock import CompetitionBackend


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"trusted audit setting {name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--rounds-dir", required=True, type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args(argv)
    try:
        backend = CompetitionBackend(
            base_url=args.backend_url,
            controller_token=_required("CLAIMS_COMPETITION_CONTROLLER_TOKEN"),
        )
        client = hippius_s3_client(
            endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
            access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
            secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"),
        )
        storage = HippiusPrivateStorage(
            client, _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
            _required("CLAIMS_HIPPIUS_LOG_BUCKET"),
        )
        review = _read_json(args.review_file) if args.review_file else None
        result = prepare_code_audit(
            args.round_id, backend=backend, storage=storage,
            directory=args.rounds_dir, review=review,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "audit_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
