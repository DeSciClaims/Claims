"""Create private final code-audit evidence and a reviewed receipt."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .code_audit import automatic_code_audit, collect_final_code_evidence, finalize_code_audit
from .package_inputs import CompetitionPackageResolver
from .publication import canonical_sha256
from .round_setup import _canonical_bytes, _write_once


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        body = handle.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("code audit input exceeds one megabyte")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("code audit input must be a JSON object")
    return value


def prepare_code_audit(
    round_id: str, *, backend: Any, storage: Any, directory: Path,
    review: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id) or not directory.is_dir():
        raise ValueError("code audit round or private directory is invalid")
    document = _read_json(directory / f"{round_id}.json")
    row = backend.get_round(round_id)
    if (document.get("round_id") != round_id or row.get("round_id") != round_id
        or not isinstance(document.get("configuration"), dict)
        or canonical_sha256(document["configuration"]) != row.get("configuration_sha256")
        or document.get("configuration_sha256") != row.get("configuration_sha256")
        or not isinstance(document.get("baseline_upload"), dict)
        or document["baseline_upload"].get("package_sha256") != row.get("baseline_package_sha256")):
        raise ValueError("code audit private round differs from frozen backend")
    packages = CompetitionPackageResolver(
        backend=backend, storage=storage,
        baseline_upload=document["baseline_upload"],
        reference_upload=document.get("reference_upload"),
    )
    evidence = collect_final_code_evidence(round_id, backend=backend, fetch_package=packages)
    digest = canonical_sha256(evidence)
    _write_once(directory / f"{round_id}.audit-evidence.json", _canonical_bytes(evidence), 0o600)
    policy = (row.get("rules_public") or {}).get("policy", {}).get("code_audit")
    if row.get("network") == "mainnet" and policy is None:
        raise ValueError("mainnet code audit has no frozen automatic thresholds")
    if policy is not None:
        if review is not None:
            raise ValueError("automatic code audit does not accept manual review")
        receipt = automatic_code_audit(evidence, policy)
        _write_once(directory / f"{round_id}.audit.json", _canonical_bytes(receipt), 0o600)
        return {"round_id": round_id, "evidence_sha256": digest, "status": "receipt_ready"}
    if review is None:
        return {"round_id": round_id, "evidence_sha256": digest, "status": "evidence_ready"}
    receipt = finalize_code_audit(evidence, review)
    _write_once(directory / f"{round_id}.audit.json", _canonical_bytes(receipt), 0o600)
    return {"round_id": round_id, "evidence_sha256": digest, "status": "receipt_ready"}
