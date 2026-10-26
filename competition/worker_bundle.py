"""Deterministic files sent from the trusted controller to a disposable guest."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def build_worker_bundle(
    *, job: Mapping[str, Any], package: bytes, pdf: bytes,
) -> dict[str, bytes]:
    """Return only the package, one PDF, request, and immutable input receipt.

    The paper-scoped Bifrost token is deliberately staged separately by the
    transport so it never enters a durable bundle or controller log.
    """
    contract = job.get("worker_contract")
    if not isinstance(contract, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(job.get("job_id", ""))):
        raise ValueError("worker bundle lacks a validated job contract")
    filename = contract.get("package_filename")
    if filename not in {"miner.whl", "miner.tar.gz"}:
        raise ValueError("worker bundle has an unsupported package name")
    request = contract.get("request")
    entrypoint = contract.get("entrypoint")
    if not isinstance(request, dict) or not isinstance(entrypoint, str):
        raise ValueError("worker bundle request or entrypoint is missing")
    paper = request.get("paper")
    if (request.get("pdf_path") != "paper.pdf"
        or not isinstance(paper, dict) or paper.get("paper_id") != job.get("paper_id")):
        raise ValueError("worker bundle request differs from the leased paper")
    package_sha256 = hashlib.sha256(package).hexdigest()
    pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    if package_sha256 != job.get("package_sha256") or pdf_sha256 != job.get("expected_pdf_sha256"):
        raise ValueError("worker bundle bytes differ from the verified inputs")
    receipt = {
        "schema_version": "claims_worker_bundle_v1",
        "job_id": job["job_id"], "paper_id": job["paper_id"],
        "package_filename": filename, "package_sha256": package_sha256,
        "pdf_sha256": pdf_sha256, "entrypoint": entrypoint,
        "request_sha256": hashlib.sha256(_json_bytes(request)).hexdigest(),
    }
    return {
        "input.json": _json_bytes(receipt),
        "request.json": _json_bytes(request),
        filename: package,
        "paper.pdf": pdf,
    }
