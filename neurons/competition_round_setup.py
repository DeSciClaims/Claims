"""Open a weekly pilot round after checking its rules and sealed baselines."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from competition.hippius import HippiusPrivateStorage, hippius_s3_client
from competition.intake_lock import CompetitionBackend
from competition.round_setup import prepare_pilot_round


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"trusted setup setting {name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--rounds-dir", type=Path, required=True)
    parser.add_argument("--backend-url", required=True)
    args = parser.parse_args(argv)
    try:
        with args.draft.open("rb") as handle:
            body = handle.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("round draft exceeds one megabyte")
        draft = json.loads(body)
        if not isinstance(draft, dict):
            raise ValueError("round draft must be a JSON object")
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
        result = prepare_pilot_round(
            draft, backend=backend, directory=args.rounds_dir,
            storage=storage,
            decision_api_key=_required(
                "JEV_API_KEY" if (
                    draft.get("configuration_spec") or draft.get("configuration") or {}
                ).get("decision_routing", {}).get("provider") == "jev"
                else "CLAIMS_COMPETITION_DECISION_API_KEY"),
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "setup_error", "error_type": type(exc).__name__}),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
