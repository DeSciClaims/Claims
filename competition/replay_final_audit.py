"""Freeze independent finalist code, output-copy, and worker-security checks."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .code_audit import (
    automatic_code_audit, collect_final_code_evidence, verify_code_audit_receipt,
)
from .copy_audit import audit_final_output_copies
from .final_stage import effective_final_stage
from .publication import canonical_sha256
from .worker_security_audit import audit_final_worker_security


def audit_replay_final(
    round_id: str, *, backend: Any,
    fetch_package: Callable[[Mapping[str, Any]], bytes],
    code_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply frozen checks to local final receipts and store one private audit.

    This does not choose a crown or emit a vector. Gold review and the
    independent publication must consume this receipt at later gates.
    """
    round_record = backend.get_round(round_id)
    stage = effective_final_stage(backend.list_stages(round_id))
    sample = [row for row in backend.list_sample(round_id) if row["stage"] == stage]
    if not sample:
        raise ValueError("replay final audit has no final paper sample")
    paper_receipts = []
    for row in sample:
        frozen = backend.get_paper_evaluation(round_id, stage, row["paper_id"])
        if frozen is None or not isinstance(frozen.get("result_private"), dict):
            raise ValueError("replay final audit has an unscored paper")
        paper_receipts.append({
            "paper_id": row["paper_id"],
            "input_sha256": frozen["input_sha256"],
            "result_sha256": canonical_sha256(frozen["result_private"]),
        })
    pairs = backend.list_stage_pairs(round_id, stage)
    evidence = collect_final_code_evidence(
        round_id, backend=backend, fetch_package=fetch_package,
    )
    code = automatic_code_audit(evidence, code_policy)
    code_passed = verify_code_audit_receipt(code, evidence, policy=code_policy)
    copy = audit_final_output_copies(round_id, backend=backend)
    security = audit_final_worker_security(round_id, backend=backend)
    baselines = [row["hotkey"] for row in backend.list_stages(round_id)[-1]["entries"]
                 if row["role"] in {"incumbent", "reference"}]
    finalists = set(code_passed)
    if (len(baselines) != 1 or not security[baselines[0]]["passed"]
        or set(copy) != finalists or not finalists <= set(security)):
        raise ValueError("replay final audit has incomplete or unsafe entrants")
    inputs = {
        "round_id": round_id, "stage": stage,
        "configuration_sha256": round_record["configuration_sha256"],
        "sample_sha256": canonical_sha256(sample),
        "paper_receipts": paper_receipts,
        "pairs_sha256": canonical_sha256(pairs),
        "code_policy_sha256": canonical_sha256(code_policy),
        "code_evidence_sha256": canonical_sha256(evidence),
    }
    result = {
        "schema_version": "claims_replay_final_audit_v1",
        "round_id": round_id, "stage": stage,
        "configuration_sha256": round_record["configuration_sha256"],
        "code_evidence": evidence, "code_receipt": code,
        "code_passed": code_passed,
        "copy_audit": copy, "worker_security_audit": security,
    }
    return backend.freeze_final_audit(
        round_id, input_sha256=canonical_sha256(inputs), result_private=result,
    )
