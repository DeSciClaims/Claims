"""Compare locked sealed packages and write a private duplicate review queue."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from competition.duplicate_candidate_setup import prepare_candidate_report
from competition.duplicate_candidates import MAX_ARCHIVE_BYTES
from competition.hippius import HippiusPrivateStorage, hippius_s3_client
from competition.intake_lock import CompetitionBackend


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"trusted candidate scanner setting {name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--rounds-dir", required=True, type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--public-base-archive", action="append", type=Path, default=[])
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
        bases = []
        for path in args.public_base_archive:
            if path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("public base archive exceeds scanner limit")
            bases.append(path.read_bytes())
        result = prepare_candidate_report(
            args.round_id, backend=backend, storage=storage,
            directory=args.rounds_dir, public_base_packages=bases,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "candidate_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
