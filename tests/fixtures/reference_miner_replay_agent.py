from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a real agent_v1 artifact for local reference-miner E2E runs.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    artifact_path = Path(os.environ.get("CLAIMS_E2E_REFERENCE_ARTIFACT", "")).expanduser()
    if not artifact_path.exists():
        raise SystemExit(f"CLAIMS_E2E_REFERENCE_ARTIFACT does not exist: {artifact_path}")

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest = {
        "runtime": "reference-miner-replay",
        "input_artifact": str(artifact_path),
        "run_dir": str(args.run_dir),
        "skill_dir": str(args.skill_dir),
        "request": str(args.request),
    }
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
