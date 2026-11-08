"""Submit the latest signed weekly competition weights from the validator wallet.

Run as a separate process after disabling the legacy extraction weight writer.
A signed legacy allocation can bridge the first competition publication.
No miner package or provider credential enters this process.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from competition.weight_heartbeat import WeeklyWeightHeartbeat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--network", choices=("mainnet", "testnet"), required=True)
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--subtensor-network", required=True)
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", required=True)
    parser.add_argument("--wallet-path", type=Path)
    parser.add_argument("--operator-hotkey", required=True)
    parser.add_argument("--cache-path", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--weight-period", type=int, default=16)
    parser.add_argument("--submission-interval-seconds", type=int, default=21600,
                        help="Seconds between refreshes; independent of chain tempo")
    parser.add_argument("--legacy-receipt", type=Path,
                        help="Signed last-extraction allocation used before the first competition result")
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--independent", action="store_true",
                        help="Read the second validator's signed independent result")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 10 <= args.poll_seconds <= 600:
        parser.error("--poll-seconds must be between 10 and 600")
    if args.submission_interval_seconds < 1:
        parser.error("--submission-interval-seconds must be positive")
    if args.independent and args.legacy_receipt:
        parser.error("--legacy-receipt is only supported by the primary heartbeat")
    # Import only after argument validation so --help works without chain setup.
    import bittensor as bt

    wallet_kwargs = {"name": args.wallet_name, "hotkey": args.wallet_hotkey}
    if args.wallet_path is not None:
        wallet_kwargs["path"] = str(args.wallet_path)
    wallet = bt.Wallet(**wallet_kwargs)
    subtensor = bt.Subtensor(network=args.subtensor_network)
    heartbeat = WeeklyWeightHeartbeat(
        network=args.network, netuid=args.netuid, backend_url=args.backend_url,
        operator_hotkey=args.operator_hotkey, wallet=wallet, subtensor=subtensor,
        cache_path=args.cache_path.expanduser(), weight_period=args.weight_period,
        independent=args.independent,
        submission_interval_seconds=args.submission_interval_seconds,
        legacy_receipt_path=args.legacy_receipt.expanduser() if args.legacy_receipt else None,
        state_path=args.state_path.expanduser() if args.state_path else None,
    )
    while True:
        try:
            result = heartbeat.step(audit_only=args.audit_only)
            print(json.dumps(result, sort_keys=True), flush=True)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}),
                  file=sys.stderr, flush=True)
            if args.once:
                return 1
        else:
            if args.once:
                return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
