"""Analyze paired compact/decision usage, Silver content and miner score changes."""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from pathlib import Path
from scripts.benchmark_equivalence_routing import read, write
from scripts.compare_decision_mode import summarize, ranks, audit_usage


def retained(record):
    return {
        cid for u in record["silver_units"] for cid in u["equivalent_candidate_ids"]
    }


def co_members(record):
    pairs = set()
    for unit in record["silver_units"]:
        ids = sorted(unit["equivalent_candidate_ids"])
        pairs.update((a, b) for i, a in enumerate(ids) for b in ids[i + 1 :])
    return pairs


def accepted(decisions):
    seen = set()
    yes = set()
    for d in decisions:
        seen.update(c["candidate_ref"] for c in d.get("candidate_findings", []))
        if d.get("accepted_candidate_ids") is not None:
            yes.update(d["accepted_candidate_ids"])
        elif d.get("selected_candidate_id"):
            yes.add(d["selected_candidate_id"])
        seen.update(d.get("rejected_candidate_ids", []))
        seen.update(yes)
    return seen, yes


def analyze(root):
    plan = read(root / "plan.json")
    papers = []
    all_events = defaultdict(list)
    score_rows = []
    provider_ids = defaultdict(list)
    for item in plan["items"]:
        pid = item["paper_id"]
        folder = root / pid
        row = {"paper_id": pid, "arms": {}}
        records = {}
        scores = {}
        decisions = {}
        for mode in ["compact", "decision"]:
            arm = folder / mode
            if not (arm / "report.json").exists():
                row["arms"][mode] = {"status": "not_finished"}
                continue
            report = read(arm / "report.json")
            events = read(arm / "usage_events.json")
            all_events[mode] += events
            audits = audit_usage(events)
            raw = [r for r in audits if r["output_matches_provider"] is not None]
            row["arms"][mode] = {
                "status": report["status"],
                "usage": summarize(events),
                "counter_audit": {
                    "events": len(audits),
                    "audited": len(raw),
                    "mismatches": sum(not r["output_matches_provider"] for r in raw),
                    "unverified": len(audits) - len(raw),
                    "successful_zero_or_missing_usage": sum(
                        e["status"] == "success"
                        and (
                            not e.get("completion_tokens") or not e.get("total_tokens")
                        )
                        for e in events
                    ),
                },
            }
            for e in events:
                meta = e.get("metadata") or {}
                ids = [
                    r.get("response_id") for r in meta.get("provider_usage_audit", [])
                ]
                ids.append(meta.get("provider_response_id"))
                for response_id in ids:
                    if response_id:
                        provider_ids[response_id].append(
                            [pid, mode, e["usage_event_id"]]
                        )
            if report["status"] == "complete":
                records[mode] = read(arm / "silver_record.json")
                decisions[mode] = read(arm / "decisions.json")
                scores[mode] = {s["miner_id"]: s for s in read(arm / "scores.json")}
                row["arms"][mode]["silver_units"] = len(records[mode]["silver_units"])
                row["arms"][mode]["equivalence_groups"] = len(
                    records[mode]
                    .get("metadata", {})
                    .get("comparison_equivalence_groups", {})
                    .get("groups", [])
                )
                row["arms"][mode]["bypassed_candidates"] = (
                    records[mode]
                    .get("metadata", {})
                    .get("comparison_equivalence_groups", {})
                    .get("bypassed_candidate_count", 0)
                )
                row["arms"][mode]["retained_contributors"] = len(
                    retained(records[mode])
                )
        if set(records) == {"compact", "decision"}:
            old, new = records["compact"], records["decision"]
            seen0, yes0 = accepted(decisions["compact"])
            seen1, yes1 = accepted(decisions["decision"])
            joint = seen0 & seen1
            row["changes"] = {
                "retained_added": sorted(retained(new) - retained(old)),
                "retained_removed": sorted(retained(old) - retained(new)),
                "co_membership_pairs_added": len(co_members(new) - co_members(old)),
                "co_membership_pairs_removed": len(co_members(old) - co_members(new)),
                "jointly_adjudicated_candidates": len(joint),
                "eligibility_admissions_changed": sorted((yes0 ^ yes1) & joint),
            }
            rank0 = ranks({m: s["score"] for m, s in scores["compact"].items()})
            rank1 = ranks({m: s["score"] for m, s in scores["decision"].items()})
            controls = {
                mode: {
                    s["miner_id"]: s
                    for s in read(
                        folder / mode / "scores_fixed_diagnostic_quality.json"
                    )
                }
                for mode in ["compact", "decision"]
            }
            for miner in sorted(set(scores["compact"]) & set(scores["decision"])):
                a, b = scores["compact"][miner], scores["decision"][miner]
                score_rows.append(
                    dict(
                        paper_id=pid,
                        miner_id=miner,
                        compact_coverage=a["coverage"],
                        decision_coverage=b["coverage"],
                        compact_quality=a["quality"],
                        decision_quality=b["quality"],
                        compact_score=a["score"],
                        decision_score=b["score"],
                        score_delta=b["score"] - a["score"],
                        compact_rank=rank0[miner],
                        decision_rank=rank1[miner],
                        compact_fixed_quality_score=controls["compact"][miner]["score"],
                        decision_fixed_quality_score=controls["decision"][miner][
                            "score"
                        ],
                    )
                )
        papers.append(row)
    result = {
        "papers": papers,
        "aggregate_usage": {m: summarize(e) for m, e in all_events.items()},
        "scores": score_rows,
        "duplicate_provider_response_ids": {
            k: v for k, v in provider_ids.items() if len(v) > 1
        },
        "scope": "Saved extraction/reference; fresh routine stages, no verbose sample. Fixed-quality results omit normal diagnostic findings in both arms.",
    }
    write(root / "analysis.json", result)
    if score_rows:
        with (root / "score_changes.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(score_rows[0]))
            writer.writeheader()
            writer.writerows(score_rows)
    stage_rows = []
    for mode, groups in result["aggregate_usage"].items():
        for stage, values in groups.items():
            stage_rows.append(dict(mode=mode, stage=stage, **values))
    if stage_rows:
        with (root / "stage_usage.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(stage_rows[0]))
            writer.writeheader()
            writer.writerows(stage_rows)
    print({m: v.get("all") for m, v in result["aggregate_usage"].items()})
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    analyze(p.parse_args().root)
