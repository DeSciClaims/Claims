"""Audit trusted firewall observations from every final worker receipt."""

from __future__ import annotations

from typing import Any, Protocol

from .final_stage import effective_final_stage


class SecurityAuditBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]: ...


def audit_final_worker_security(round_id: str, *, backend: SecurityAuditBackend) -> dict[str, dict[str, Any]]:
    """Fail closed if a final worker lacks a counter or tried denied egress."""
    stage = effective_final_stage(backend.list_stages(round_id))
    ledgers = [row for row in backend.list_stages(round_id) if row.get("stage") == stage]
    if len(ledgers) != 1:
        raise ValueError("security audit has no unique final stage")
    entries = ledgers[0]["entries"]
    expected = {row["hotkey"]: row["package_sha256"] for row in entries}
    if len(expected) != len(entries) or not expected:
        raise ValueError("security audit final entrants are malformed")
    papers = [row["paper_id"] for row in backend.list_sample(round_id) if row["stage"] == stage]
    if not papers or len(set(papers)) != len(papers):
        raise ValueError("security audit final sample is empty or duplicated")
    totals = {hotkey: {"paper_count": 0, "denied_papers": 0, "denied_packets": 0}
              for hotkey in expected}
    for paper_id in papers:
        jobs = backend.list_paper_jobs(round_id, stage, paper_id)
        if len(jobs) != len(expected) or {row["hotkey"] for row in jobs} != set(expected):
            raise ValueError("security audit final job grid is incomplete")
        for job in jobs:
            hotkey = job["hotkey"]
            if (job.get("status") != "completed"
                or job.get("package_sha256") != expected[hotkey]
                or job.get("paper_id") != paper_id
                or job.get("stage") != stage):
                raise ValueError("security audit job differs from the frozen final")
            result = job.get("result_private")
            denied = result.get("denied_egress_packets") if isinstance(result, dict) else None
            if isinstance(denied, bool) or not isinstance(denied, int) or denied < 0:
                raise ValueError("security audit job lacks a trusted denied-egress counter")
            if (result.get("status") == "security_violation") != bool(denied):
                raise ValueError("security audit job status differs from denied egress")
            totals[hotkey]["paper_count"] += 1
            totals[hotkey]["denied_packets"] += denied
            totals[hotkey]["denied_papers"] += int(denied > 0)
    return {hotkey: {**counts, "passed": counts["denied_packets"] == 0}
            for hotkey, counts in sorted(totals.items())}
