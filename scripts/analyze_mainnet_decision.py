"""Compare historical compact batches with decision-only saved-input replays."""

from __future__ import annotations
import argparse
from collections import Counter
import csv
import hashlib
from pathlib import Path
from scripts.benchmark_equivalence_routing import read, write
from scripts.compare_decision_mode import summarize, ranks, audit_usage
from scripts.analyze_decision_replay import retained, co_members, accepted
from validator.agent_v1.batch_scoring import score_batch
from validator.agent_v1.comparison_models import SilverScoreBreakdown


def routine(events):
    return [
        e
        for e in events
        if e["stage_key"] != "reference_miner"
        and e.get("usage_source") != "shared_diagnostic_paper"
    ]


def usage(events):
    result = summarize(events)
    for stage, values in result.items():
        from scripts.compare_decision_mode import stage_name

        rows = (
            events
            if stage == "all"
            else [e for e in events if stage_name(e["stage_key"]) == stage]
        )
        values["input_plus_output_tokens"] = sum(
            (e.get("prompt_tokens") or 0) + (e.get("completion_tokens") or 0)
            for e in rows
        )
        values["reasoning_exceeds_completion_events"] = sum(
            (e.get("reasoning_tokens") or 0) > (e.get("completion_tokens") or 0)
            for e in rows
        )
        values["cost_kinds"] = dict(Counter(e.get("cost_kind") for e in rows))
        values["reasoning_reported_when_disabled_events"] = sum(
            e.get("metadata", {}).get("reasoning_enabled") is False
            and bool(e.get("reasoning_tokens"))
            for e in rows
        )
        values["cache_read_tokens"] = sum(e.get("cache_read_tokens") or 0 for e in rows)
        values["cache_read_counter_events"] = sum(
            e.get("cache_read_tokens") is not None for e in rows
        )
    return result


def aggregate(batch, by_paper, papers):
    scores = []
    for miner in batch["miners"]:
        mid = miner["miner_id"]
        originals = {s["paper_id"]: s for s in miner["paper_scores"]}
        for pid in papers:
            score = by_paper.get(pid, {}).get(mid)
            if score is None:
                old = originals.get(pid)
                if (
                    old
                    and old.get("metadata", {}).get("code")
                    == "missing_paper_submission"
                ):
                    score = old
                else:
                    raise ValueError(f"Missing non-submission score: {mid} {pid}")
            scores.append(SilverScoreBreakdown.model_validate(score))
    result = score_batch(
        batch_id=batch["batch_id"],
        paper_scores=scores,
        expected_paper_ids=batch["expected_paper_ids"],
        eligible_paper_ids=sorted(papers),
        validator_failed_paper_ids=sorted(
            set(batch["expected_paper_ids"]) - set(papers)
        ),
        selection_lanes={
            m["miner_id"]: m.get("selection_lane") for m in batch["miners"]
        },
        reward_exclusions={
            m["miner_id"]: m.get("reward_exclusion_reason") or "historical_exclusion"
            for m in batch["miners"]
            if not m.get("reward_eligible", True)
        },
    )
    return {
        m.miner_id: dict(score=m.batch_score, raw_rank=m.raw_rank, rank=m.rank)
        for m in result.miners
    }


def analyze(inputs, replay, output):
    output.mkdir(parents=True, exist_ok=True)
    runs = []
    stage_rows = []
    score_rows = []
    paper_rows = []
    admissions = []
    for entry in read(inputs / "runs.json"):
        rid = entry["run_id"]
        old = inputs / rid
        new = replay / rid
        batch = read(old / "historical/batch_score_result.json")
        backup = read(old / "historical/usage_events.json")
        events = backup["events"]
        ids = [e["usage_event_id"] for e in events]
        if len(ids) != len(set(ids)) or len(ids) != backup["expected_event_count"]:
            raise ValueError("Historical journal incomplete or duplicated")
        digest = hashlib.sha256()
        for event_id in ids:
            digest.update(str(event_id).encode("utf-8"))
            digest.update(b"\n")
        if digest.hexdigest() != backup["event_ids_sha256"]:
            raise ValueError("Historical event journal checksum mismatch")
        suite_path = new / "suite_status.json"
        suite = read(suite_path) if suite_path.exists() else {}
        paper_statuses = {p["paper_id"]: p for p in suite.get("papers", [])}
        before = routine(events)
        after = []
        complete = []
        old_scores = {}
        new_scores = {}
        fixed_scores = {}
        papers = []
        for m in batch["miners"]:
            for s in m["paper_scores"]:
                old_scores.setdefault(s["paper_id"], {})[m["miner_id"]] = s
        for item in read(old / "manifest.json"):
            pid = item["paper_id"]
            arm = new / pid / "decision"
            report = (
                read(arm / "report.json")
                if (arm / "report.json").exists()
                else paper_statuses.get(pid, {})
            )
            ev = (
                read(arm / "usage_events.json")
                if (arm / "usage_events.json").exists()
                else []
            )
            after += ev
            row = dict(
                run_id=rid,
                paper_id=pid,
                historical_complete=pid in batch["eligible_paper_ids"],
                decision_status=report.get("status", "not_complete"),
                miner_count=len(item["miner_ids"]),
                failure_reason=report.get("traceback", "").splitlines()[-1]
                if report.get("traceback")
                else report.get("reason", ""),
            )
            if report.get("status") == "complete":
                complete.append(pid)
                sr = read(arm / "silver_record.json")
                new_scores[pid] = {s["miner_id"]: s for s in read(arm / "scores.json")}
                fixed_scores[pid] = {
                    s["miner_id"]: s
                    for s in read(arm / "scores_historical_diagnostics.json")
                }
                row.update(
                    decision_units=len(sr["silver_units"]),
                    technical_unresolved_cases=sr.get("metadata", {})
                    .get("eligibility_selection_adjudication", {})
                    .get("technical_unresolved_case_count", 0),
                    decision_contributors=len(retained(sr)),
                    decision_importance=dict(
                        Counter(u["importance"] for u in sr["silver_units"])
                    ),
                    decision_groups=len(
                        sr.get("metadata", {})
                        .get("comparison_equivalence_groups", {})
                        .get("groups", [])
                    ),
                )
                original = old / "historical/silver" / pid / "silver_record.json"
                if original.exists():
                    osr = read(original)
                    added = retained(sr) - retained(osr)
                    removed = retained(osr) - retained(sr)
                    row.update(
                        historical_units=len(osr["silver_units"]),
                        historical_groups=len(
                            osr.get("metadata", {})
                            .get("comparison_equivalence_groups", {})
                            .get("groups", [])
                        ),
                        historical_bypassed_candidates=osr.get("metadata", {})
                        .get("comparison_equivalence_groups", {})
                        .get("bypassed_candidate_count", 0),
                        decision_bypassed_candidates=sr.get("metadata", {})
                        .get("comparison_equivalence_groups", {})
                        .get("bypassed_candidate_count", 0),
                        historical_contributors=len(retained(osr)),
                        retained_added=len(added),
                        retained_removed=len(removed),
                        co_membership_added=len(co_members(sr) - co_members(osr)),
                        co_membership_removed=len(co_members(osr) - co_members(sr)),
                        historical_importance=dict(
                            Counter(u["importance"] for u in osr["silver_units"])
                        ),
                    )
                    for cid in sorted(added | removed):
                        admissions.append(
                            dict(
                                run_id=rid,
                                paper_id=pid,
                                candidate_id=cid,
                                change="added" if cid in added else "removed",
                            )
                        )
            papers.append(row)
            paper_rows.append(row)
        historical_complete = set(batch["eligible_paper_ids"])
        matched = historical_complete & set(complete)
        verbose = batch.get("eligibility_output_sampling", {}).get("verbose_paper_id")
        scopes = {}
        for scope, ps in [
            ("full_assigned", set(batch["expected_paper_ids"])),
            ("matched_complete", matched),
            ("matched_routine", matched - {verbose}),
        ]:
            scopes[scope] = {
                mode: usage([e for e in rows if e.get("paper_id") in ps])
                for mode, rows in [("historical", before), ("decision", after)]
            }
            for mode, stages in scopes[scope].items():
                for stage, values in stages.items():
                    stage_rows.append(
                        dict(
                            run_id=rid,
                            scope=scope,
                            mode=mode,
                            stage=stage,
                            **{k: v for k, v in values.items() if k != "cost_kinds"},
                        )
                    )
        if matched:
            shared_old = aggregate(batch, old_scores, matched)
            shared_new = aggregate(batch, new_scores, matched)
            shared_fixed = aggregate(batch, fixed_scores, matched)
            observed_new = aggregate(batch, new_scores, set(complete))
            historical_full = {m["miner_id"]: m for m in batch["miners"]}
            for mid in shared_old:
                score_rows.append(
                    dict(
                        run_id=rid,
                        miner_id=mid,
                        matched_papers=len(matched),
                        historical_observed_score=historical_full[mid]["batch_score"],
                        historical_observed_rank=historical_full[mid]["rank"],
                        decision_observed_score=observed_new[mid]["score"],
                        decision_observed_rank=observed_new[mid]["rank"],
                        matched_historical_score=shared_old[mid]["score"],
                        matched_decision_score=shared_new[mid]["score"],
                        matched_historical_rank=shared_old[mid]["rank"],
                        matched_decision_rank=shared_new[mid]["rank"],
                        decision_historical_diagnostics_score=shared_fixed[mid][
                            "score"
                        ],
                        decision_historical_diagnostics_rank=shared_fixed[mid]["rank"],
                    )
                )
        after_ids = [e["usage_event_id"] for e in after]
        if len(after_ids) != len(set(after_ids)):
            raise ValueError("Duplicate decision usage event IDs")
        response_ids = [
            e.get("metadata", {}).get("provider_response_id") for e in after
        ]
        duplicate_responses = {
            key: count
            for key, count in Counter(response_ids).items()
            if key and count > 1
        }
        audits = audit_usage(after)
        issues = [
            {
                k: e.get(k)
                for k in [
                    "usage_event_id",
                    "paper_id",
                    "stage_key",
                    "status",
                    "completion_tokens",
                    "total_tokens",
                ]
            }
            for e in before
            if e.get("completion_tokens") is None or e.get("total_tokens") is None
        ]
        status = (
            read(new / "suite_status.json")
            if (new / "suite_status.json").exists()
            else {}
        )
        runs.append(
            dict(
                run_id=rid,
                assigned_papers=len(batch["expected_paper_ids"]),
                historical_completed=len(historical_complete),
                decision_completed=len(complete),
                matched_completed=len(matched),
                verbose_paper_id=verbose,
                status=status.get("status", "not_started"),
                historical_events=len(events),
                historical_journal_count_verified=True,
                shared_diagnostic_bookkeeping_events=sum(
                    e.get("usage_source") == "shared_diagnostic_paper" for e in events
                ),
                historical_missing_counter_events=issues,
                decision_counter_audit=dict(
                    events=len(audits),
                    duplicate_provider_response_ids=duplicate_responses,
                    verified=sum(r["output_matches_provider"] is True for r in audits),
                    mismatches=sum(
                        r["output_matches_provider"] is False for r in audits
                    ),
                    unverified=sum(
                        r["output_matches_provider"] is None for r in audits
                    ),
                ),
                usage_scopes=scopes,
                papers=papers,
            )
        )
    result = dict(
        runs=runs,
        score_comparison=score_rows,
        scope="Historical compact versus fresh decision; no compact rerun. Full assigned and matched complete/routine scopes; reference generation and bookkeeping excluded. Historical counters with missing values remain lower bounds. Observed and matched-scope rankings are distinct.",
    )
    write(output / "analysis.json", result)
    for name, rows in [
        ("stage_usage.csv", stage_rows),
        ("miner_rankings.csv", score_rows),
        ("paper_changes.csv", paper_rows),
        ("contributor_changes.csv", admissions),
    ]:
        if not rows:
            continue
        keys = list(dict.fromkeys(k for row in rows for k in row))
        with (output / name).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--replay", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    analyze(args.inputs, args.replay, args.output)


if __name__ == "__main__":
    main()
