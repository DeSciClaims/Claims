"""Guest-side entrypoint for one verified bundle and one paper-scoped key.

Run only inside the disposable worker VM. The VM image supplies a pinned,
offline wheelhouse and this trusted harness. The transport keeps input_dir
unwritable by the miner user and makes result_dir writable; miner code is installed after bundle
verification. The transport collects result.json and install/run log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .worker import run_one_paper


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("worker bundle JSON must be an object")
    return value


def run_bundle(
    *, input_dir: Path, result_dir: Path, wheelhouse: Path, inference_base_url: str,
    paper_key_file: Path, install_timeout_seconds: int = 600,
    run_timeout_seconds: int = 1800,
) -> dict[str, Any]:
    receipt = _read_json(input_dir / "input.json")
    request_path = input_dir / "request.json"
    request = _read_json(request_path)
    filename = receipt.get("package_filename")
    paper = request.get("paper")
    if (receipt.get("schema_version") != "claims_worker_bundle_v1"
        or filename not in {"miner.whl", "miner.tar.gz"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("job_id", "")))
        or not isinstance(paper, dict) or paper.get("paper_id") != receipt.get("paper_id")
        or request.get("pdf_path") != "paper.pdf"):
        raise ValueError("worker bundle receipt is invalid")
    for name, digest in (
        (filename, receipt.get("package_sha256")),
        ("paper.pdf", receipt.get("pdf_sha256")),
        ("request.json", receipt.get("request_sha256")),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError("worker bundle has an invalid input digest")
        if hashlib.sha256((input_dir / name).read_bytes()).hexdigest() != digest:
            raise ValueError("worker bundle input digest mismatch")
    key = paper_key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("paper-scoped inference key is missing")
    result_dir.mkdir(parents=True, exist_ok=True)
    output_path = result_dir / "output.json"
    log_path = result_dir / "worker.log"
    result = run_one_paper(
        package_path=input_dir / filename,
        entrypoint=str(receipt["entrypoint"]), request_path=request_path,
        output_path=output_path, work_dir=result_dir / "work", log_path=log_path,
        wheelhouse=wheelhouse, inference_base_url=inference_base_url,
        paper_key=key, install_timeout_seconds=install_timeout_seconds,
        run_timeout_seconds=run_timeout_seconds,
    )
    artifact = _read_json(output_path) if result.status == "completed" else None
    summary = {
        "schema_version": "claims_worker_result_v1",
        "job_id": receipt["job_id"], "status": result.status,
        "artifact": artifact, "detail": result.detail,
        "elapsed_seconds": result.elapsed_seconds,
    }
    temporary = result_dir / "result.json.tmp"
    temporary.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, result_dir / "result.json")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one competition paper in a disposable guest")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--inference-base-url", required=True)
    parser.add_argument("--paper-key-file", type=Path, required=True)
    args = parser.parse_args()
    run_bundle(
        input_dir=args.input_dir, result_dir=args.result_dir,
        wheelhouse=args.wheelhouse,
        inference_base_url=args.inference_base_url,
        paper_key_file=args.paper_key_file,
    )


if __name__ == "__main__":
    main()
