import hashlib
import io
import json
import zipfile

import pytest

from competition.code_audit import (
    collect_final_code_evidence, finalize_code_audit, verify_code_audit_receipt,
)
from competition.code_audit_setup import prepare_code_audit
from competition.publication import canonical_sha256


def _artifact(paper_id, statement):
    return {
        "paper": {"paper_id": paper_id},
        "logic": {"claims": [{
            "claim_id": "c1", "statement": statement, "conditions": "",
            "status": "supported", "falsification_criteria": "",
        }]},
        "evidence": {},
        "trace": {"node_id": "root", "node_type": "paper", "summary": ""},
        "src": {},
    }


def _wheel():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("example_miner/__init__.py", "def extract(request):\n    return request\n")
    return stream.getvalue()


class Backend:
    def __init__(self, package_hash):
        self.package_hash = package_hash
        self.build_failure = False

    def list_stages(self, _):
        return [{"stage": "final", "entries": [
            {"hotkey": "baseline", "role": "incumbent", "package_sha256": "b" * 64},
            {"hotkey": "miner", "role": "challenger", "package_sha256": self.package_hash},
        ]}]

    def list_sample(self, _):
        return [{"stage": "final", "paper_id": paper_id}
                for paper_id in ("paper-1", "paper-2")]

    def list_paper_jobs(self, round_id, stage, paper_id):
        jobs = []
        for hotkey, package_hash in (("baseline", "b" * 64), ("miner", self.package_hash)):
            status = ("build_failed" if self.build_failure and hotkey == "miner"
                      and paper_id == "paper-2" else "completed")
            jobs.append({
                "round_id": round_id, "stage": stage, "paper_id": paper_id,
                "hotkey": hotkey, "package_sha256": package_hash,
                "status": "completed",
                "result_private": {
                    "status": status,
                    "artifact": _artifact(paper_id, "same finding") if status == "completed" else None,
                    "token_limit": 1000, "prompt_and_completion_tokens": 100,
                    "denied_egress_packets": 0,
                },
            })
        return jobs

    def get_round(self, round_id):
        return {"round_id": round_id, "incumbent_hotkey": "baseline",
                "configuration_sha256": canonical_sha256({"pilot": True}),
                "baseline_package_sha256": "b" * 64}

    def list_submissions(self, _):
        return [{"hotkey": "miner", "package_sha256": self.package_hash,
                 "status": "accepted"}]


def test_code_audit_binds_review_to_packages_and_worker_receipts():
    package = _wheel()
    package_hash = hashlib.sha256(package).hexdigest()
    backend = Backend(package_hash)
    evidence = collect_final_code_evidence(
        "week1", backend=backend,
        fetch_package=lambda job: package,
    )
    row = evidence["packages"][0]
    assert row["package_sha256"] == package_hash
    assert row["build_passed"] is True
    assert row["final_job_count"] == 2
    assert row["max_same_statement_set_papers"] == 2
    review = {
        "round_id": "week1", "evidence_sha256": canonical_sha256(evidence),
        "reviewer": "claims-reviewer", "packages": [{
            "hotkey": "miner", "package_sha256": package_hash,
            "checks": {name: True for name in (
                "hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation",
            )},
        }],
    }
    receipt = finalize_code_audit(evidence, review)
    assert verify_code_audit_receipt(receipt, evidence) == {"miner": True}
    backend.build_failure = True
    changed = collect_final_code_evidence(
        "week1", backend=backend, fetch_package=lambda job: package,
    )
    assert changed["packages"][0]["build_passed"] is False
    with pytest.raises(ValueError, match="frozen evidence"):
        verify_code_audit_receipt(receipt, changed)
    with pytest.raises(ValueError, match="frozen final"):
        collect_final_code_evidence(
            "week1", backend=backend, fetch_package=lambda job: b"wrong",
        )


def test_review_cannot_skip_or_invent_a_check():
    package = _wheel()
    backend = Backend(hashlib.sha256(package).hexdigest())
    evidence = collect_final_code_evidence(
        "week1", backend=backend, fetch_package=lambda job: package,
    )
    base = {"round_id": "week1", "evidence_sha256": canonical_sha256(evidence),
            "reviewer": "claims-reviewer"}
    with pytest.raises(ValueError, match="cover finalists"):
        finalize_code_audit(evidence, {**base, "packages": []})
    with pytest.raises(ValueError, match="sealed finalist"):
        finalize_code_audit(evidence, {**base, "packages": [{
            "hotkey": "miner", "package_sha256": evidence["packages"][0]["package_sha256"],
            "checks": {"hardcoded_output": True},
        }]})
    evidence["packages"][0]["denied_egress_packets"] = 1
    with pytest.raises(ValueError, match="denied worker egress"):
        finalize_code_audit(evidence, {
            "round_id": "week1", "evidence_sha256": canonical_sha256(evidence),
            "reviewer": "claims-reviewer", "packages": [{
                "hotkey": "miner", "package_sha256": backend.package_hash,
                "checks": {name: True for name in (
                    "hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation",
                )},
            }],
        })


def test_audit_setup_writes_private_review_bound_receipt(tmp_path):
    package = _wheel()
    package_hash = hashlib.sha256(package).hexdigest()
    backend = Backend(package_hash)
    (tmp_path / "week1.json").write_text(json.dumps({
        "round_id": "week1", "configuration": {"pilot": True},
        "configuration_sha256": canonical_sha256({"pilot": True}),
        "baseline_upload": {"hotkey": "baseline", "package_sha256": "b" * 64},
    }))

    class Storage:
        def package_for_submission(self, upload):
            assert upload["package_sha256"] == package_hash
            return package

    first = prepare_code_audit(
        "week1", backend=backend, storage=Storage(), directory=tmp_path,
    )
    assert first["status"] == "evidence_ready"
    evidence = json.loads((tmp_path / "week1.audit-evidence.json").read_text())
    review = {"round_id": "week1", "evidence_sha256": first["evidence_sha256"],
              "reviewer": "claims-reviewer", "packages": [{
                  "hotkey": "miner", "package_sha256": package_hash,
                  "checks": {name: True for name in (
                      "hardcoded_output", "evaluation_context", "cap_evasion", "fragmentation",
                  )},
              }]}
    final = prepare_code_audit(
        "week1", backend=backend, storage=Storage(), directory=tmp_path, review=review,
    )
    assert final["status"] == "receipt_ready"
    receipt = json.loads((tmp_path / "week1.audit.json").read_text())
    assert verify_code_audit_receipt(receipt, evidence) == {"miner": True}
    assert (tmp_path / "week1.audit.json").stat().st_mode & 0o777 == 0o600
    backend.build_failure = True
    with pytest.raises(ValueError, match="already differs"):
        prepare_code_audit("week1", backend=backend, storage=Storage(),
                           directory=tmp_path, review=review)
