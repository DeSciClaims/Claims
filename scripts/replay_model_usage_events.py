from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from neurons.backend_client import ClaimsBackendClient
from neurons.model_usage_upload import prepare_model_usage_backup, read_model_usage_backup, upload_model_usage_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a validator model-usage backup with immutable resumable chunks.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--backend-url", default=os.getenv("CLAIMS_BACKEND_URL", ""))
    parser.add_argument("--network", default=os.getenv("CLAIMS_NETWORK", "testnet"))
    parser.add_argument("--wallet-name", default="claims-test-validator")
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--chunk-size", type=int, default=100)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")
    backend_url = str(args.backend_url or os.getenv("CLAIMS_BACKEND_URL", "")).strip()
    if not backend_url:
        parser.error("--backend-url or CLAIMS_BACKEND_URL is required")

    raw = read_model_usage_backup(args.backup)
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    if not events:
        parser.error(f"backup contains no events: {args.backup}")
    first = events[0]
    run_id = str(raw.get("run_id") or first.get("run_id") or "")
    batch_id = str(raw.get("batch_id") or first.get("batch_id") or "")
    network = str(raw.get("network") or first.get("network") or args.network)

    from bittensor import Wallet

    wallet = Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    client = ClaimsBackendClient(
        base_url=backend_url,
        wallet=wallet,
        network=network,
        timeout_seconds=float(os.getenv("CLAIMS_BACKEND_TIMEOUT", "60")),
        max_retries=int(os.getenv("CLAIMS_BACKEND_RETRIES", "3")),
        retry_backoff_seconds=float(os.getenv("CLAIMS_BACKEND_RETRY_BACKOFF", "2")),
    )
    prepare_model_usage_backup(
        args.backup,
        events=events,
        network=network,
        run_id=run_id,
        batch_id=batch_id,
    )
    summary = upload_model_usage_backup(args.backup, backend_client=client, chunk_size=args.chunk_size)
    print(json.dumps({"run_id": run_id, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
