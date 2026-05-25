from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulator.mock_miner import run_mock_miner
from simulator.mock_validator import score_payload
from simulator.protocol import build_task_envelope
from simulator.scoring import validate_against_schema


def main() -> int:
    task = build_task_envelope()
    payload = run_mock_miner(task, variant="valid")
    schema_errors = validate_against_schema(payload, "extraction.schema.json")
    report = score_payload(payload)

    print("TASK")
    print(json.dumps(task, indent=2))
    print()
    print("PAYLOAD")
    print(json.dumps(payload, indent=2))
    print()
    print("SCHEMA_ERRORS")
    print(json.dumps(schema_errors, indent=2))
    print()
    print("SCORE_REPORT")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
