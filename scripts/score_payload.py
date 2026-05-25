from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulator.mock_validator import score_payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a miner payload with the demo validator.")
    parser.add_argument("payload", type=Path, help="Path to the extraction payload JSON file")
    args = parser.parse_args()

    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    report = score_payload(payload)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
