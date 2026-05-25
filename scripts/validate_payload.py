from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulator.scoring import validate_against_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a JSON payload against a demo schema.")
    parser.add_argument("payload", type=Path, help="Path to the JSON payload file")
    parser.add_argument("schema", type=Path, help="Path to the schema file")
    args = parser.parse_args()

    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    errors = validate_against_schema(payload, args.schema.name)

    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1

    print("VALID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
