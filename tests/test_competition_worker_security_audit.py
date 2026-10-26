import pytest

from competition.worker_security_audit import audit_final_worker_security


class Backend:
    def __init__(self, denied=0):
        self.denied = denied

    def get_round(self, _):
        return {"incumbent_hotkey": "incumbent"}

    def list_stages(self, _):
        return [{"stage": "final", "entries": [
            {"hotkey": "incumbent", "role": "incumbent", "package_sha256": "a" * 64},
            {"hotkey": "challenger", "role": "challenger", "package_sha256": "b" * 64},
        ]}]

    def list_sample(self, _):
        return [{"stage": "final", "paper_id": "paper-1"},
                {"stage": "final", "paper_id": "paper-2"}]

    def list_paper_jobs(self, _, stage, paper_id):
        return [{"stage": stage, "paper_id": paper_id, "hotkey": hotkey,
                 "package_sha256": digest, "status": "completed",
                 "result_private": {"status": "security_violation" if denied else "completed",
                                    "denied_egress_packets": denied}}
                for hotkey, digest, denied in (
                    ("incumbent", "a" * 64, 0),
                    ("challenger", "b" * 64,
                     self.denied if paper_id == "paper-2" else 0),
                )]


def test_security_audit_counts_denied_packets_and_fails_the_challenger():
    result = audit_final_worker_security("week", backend=Backend(denied=3))
    assert result["incumbent"] == {"paper_count": 2, "denied_papers": 0,
                                   "denied_packets": 0, "passed": True}
    assert result["challenger"] == {"paper_count": 2, "denied_papers": 1,
                                    "denied_packets": 3, "passed": False}


def test_security_audit_rejects_missing_trusted_counter():
    backend = Backend()
    original = backend.list_paper_jobs

    def missing(*args):
        jobs = original(*args)
        jobs[1]["result_private"].pop("denied_egress_packets")
        return jobs

    backend.list_paper_jobs = missing
    with pytest.raises(ValueError, match="trusted denied-egress counter"):
        audit_final_worker_security("week", backend=backend)
