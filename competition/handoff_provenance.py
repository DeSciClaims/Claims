"""Verify preserved final scores through immutable, audited rule revisions."""
from __future__ import annotations

from .publication import canonical_sha256


def verify_preserved_scores(backend, round_record, bundle, rows):
    """Allow old score hashes only with their exact frozen carry-forward proof."""
    rid = bundle["round_id"]
    revision = bundle.get("rules_revision", 1)
    if (backend is None or type(revision) is not int or revision < 2
        or round_record.get("rules_revision", 1) != revision):
        raise ValueError("handoff score provenance requires its audited rules revisions")
    history = backend.rules_amendments(rid)
    amendments = {a["to_revision"]: a for a in history["amendments"]}
    if (history["rules_revision"] != revision or len(amendments) != len(history["amendments"])
        or revision not in amendments
        or amendments[revision].get("amendment_sha256") != bundle.get("rules_revision_sha256")):
        raise ValueError("handoff amendment history differs from signed publication")
    stages = {r["stage"] for r in rows}
    papers = {}
    for stage in stages:
        for paper in backend.list_evaluation_provenance(rid, stage):
            key = (paper["stage"], paper["paper_id"])
            if paper.get("round_id") != rid or paper["stage"] != stage or key in papers:
                raise ValueError("handoff paper provenance has duplicate or foreign papers")
            papers[key] = paper
    snapshots = {revision: round_record["rules_public"]}

    def rules_at(number):
        if number not in snapshots:
            snapshots[number] = backend.rules_snapshot(rid, number)
        rules = snapshots[number]
        if (rules.get("round_id") != rid or rules.get("network") != bundle["network"]
            or canonical_sha256(rules.get("configuration")) != rules.get("configuration_sha256")):
            raise ValueError("handoff historical rules have invalid identity or configuration")
        return rules

    verified = set()
    for row in rows:
        if row["configuration_sha256"] == bundle["configuration_sha256"]:
            continue
        key = (row["stage"], row["paper_id"], row["configuration_sha256"])
        if key in verified:
            continue
        paper = papers.get((row["stage"], row["paper_id"]))
        if paper is None:
            raise ValueError("handoff preserved score has no frozen paper provenance")
        origin = paper.get("produced_rules_revision")
        carried = paper.get("carried_forward") or {}
        if (type(origin) is not int or not 1 <= origin < revision
            or paper.get("configuration_sha256") != row["configuration_sha256"]
            or carried.get("from_revision") != origin or carried.get("to_revision") != revision
            or carried.get("configuration_sha256") != row["configuration_sha256"]
            or not paper.get("input_sha256") or carried.get("input_sha256") != paper["input_sha256"]
            or rules_at(origin)["configuration_sha256"] != row["configuration_sha256"]):
            raise ValueError("handoff preserved score differs from its producing revision")
        for number in range(origin + 1, revision + 1):
            amendment = amendments.get(number, {})
            before, after = rules_at(number - 1), rules_at(number)
            if (amendment.get("round_id") != rid or amendment.get("from_revision") != number - 1
                or amendment.get("previous_rules_sha256") != canonical_sha256(before)
                or amendment.get("rules_sha256") != canonical_sha256(after)
                or amendment.get("previous_configuration_sha256") != before["configuration_sha256"]
                or amendment.get("configuration_sha256") != after["configuration_sha256"]
                or amendment.get("resume_mode") != "resume" or amendment.get("restart_stage") is not None
                or amendment.get("preserved_paper_count", 0) < 1):
                raise ValueError("handoff preserved score has a broken amendment chain")
        verified.add(key)
