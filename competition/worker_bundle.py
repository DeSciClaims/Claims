"""Deterministic files sent from the trusted controller to a disposable guest."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .worker_contract import validated_package_filename


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
    validated_package_filename(
        filename, "wheel" if isinstance(filename, str) and filename.endswith(".whl")
        else "tar.gz",
    )
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
    additional = {}
    if request.get("output_schema") == "scientific_result_v1.1":
        from validator.scientific_result_v1.source_tasks.packages import SCHEMA_PATH
        schema = SCHEMA_PATH.read_bytes()
        sources = _json_bytes({"source_scope_policy":"supplied_sources_only", "sources":[{
            "source_id":"S001", "role":"MAIN", "path":"paper.pdf", "sha256":pdf_sha256,
            "byte_length":len(pdf), "media_type":"application/pdf"}]})
        supplied=job.get("scientific_inputs")
        extra={}
        if supplied is not None:
            if supplied["files"].get("paper.pdf")!=pdf:raise ValueError("Scientific source packet differs from main PDF")
            for name,data in supplied["files"].items():
                if name=="paper.pdf":continue
                if not re.fullmatch(r"supplied-source-S[0-9]+\.(pdf|txt|html|csv)",name):raise ValueError("Unsafe supplied source path")
                extra[name]=data
            sources=_json_bytes(supplied["manifest"])
            for doc in supplied["manifest"]["sources"]:
                body=pdf if doc["path"]=="paper.pdf" else extra[doc["path"]]
                if hashlib.sha256(body).hexdigest()!=doc["sha256"] or len(body)!=doc["byte_length"]:raise ValueError("Supplied source bytes mismatch")
        additional = {"sources.json":sources, "claims_extraction.schema.json":schema, **extra}
        receipt["scientific_input_sha256"] = {name:hashlib.sha256(data).hexdigest() for name,data in additional.items()}
    return {
        **additional,
        "input.json": _json_bytes(receipt),
        "request.json": _json_bytes(request),
        filename: package,
        "paper.pdf": pdf,
    }
