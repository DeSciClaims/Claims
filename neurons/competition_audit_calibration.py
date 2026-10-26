"""Offline week-minus-one finalist code-audit calibration report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from competition.audit_calibration import evaluate_audit_calibration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Private JSON with frozen evidence and independent labels")
    parser.add_argument("--output", type=Path,
                        help="Aggregate report JSON; stdout when omitted")
    args = parser.parse_args()
    with args.input.open("rb") as handle:
        body = handle.read(20_000_001)
    if len(body) > 20_000_000:
        parser.error("calibration input exceeds 20 MB")
    report = evaluate_audit_calibration(json.loads(body))
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered)


if __name__ == "__main__":
    main()
