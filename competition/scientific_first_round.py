"""Explicit, round-bound conservative scoring when first-round review is incomplete.

This is a separate score receipt, never a rewrite of a strict HOLD as FROZEN.
The coverage target describes evaluation completion; it cannot veto a score.
"""
import json
from collections import Counter, defaultdict

from validator.scientific_result_v1.source_tasks.contracts import (
    FileRef, SampledSilverReceipt, SampleManifest, SourceFrame,
    TaskCard, ScientificVerdict, JudgeRunReceipt,
)
from validator.scientific_result_v1.source_tasks.failure_tolerance import policy, project
from validator.scientific_result_v1.source_tasks.store import canonical

KIND = "claims_first_round_conservative_v1"


def first_round_policy(round_id):
    if not isinstance(round_id, str) or not round_id:
        raise ValueError("First-round scoring needs an explicit round ID")
    return {"kind": KIND, "round_id": round_id, "coverage_target": "4/5",
            "coverage_threshold_behavior": "advisory", "unresolved_credit": "zero",
            "retain_unresolved_denominators": True, "ranking_score": "conservative_lower_bound"}


def validate_policy(value, round_id):
    if not isinstance(value,dict) or canonical(value) != canonical(first_round_policy(round_id)):
        raise ValueError("Invalid or differently scoped first-round scoring policy")
    return value


def configured_policy(configuration, round_id):
    value = configuration.get("scientific_scoring", {}).get("first_round_scoring")
    return None if value is None else validate_policy(value, round_id)


def projection_key(receipt_sha256):
    return "failure-tolerance:" + receipt_sha256


def _validate_cases(store, receipt, events):
    """No duplicated case weight, invented successful case, or changed verdict bit."""
    aliases = {s.output_alias for s in receipt.paper_scores}
    cards = {c.task_id: c for c in (store.load(r, TaskCard) for r in receipt.task_cards)}
    verdicts = [store.load(r, ScientificVerdict) for r in receipt.scientific_verdicts]
    seen = set()
    for event in events:
        if event["stage"] != "SCIENTIFIC_CASE":
            continue
        alias, key = event["output_alias"], event["key"]
        if alias not in aliases or (alias, key) in seen or key not in cards:
            raise ValueError("Unknown or duplicate partial-score case")
        seen.add((alias, key))
        card = cards[key]
        if (event["task_kind"], event["unit_id"]) != (card.task_kind, card.source_bucket_id or card.tile_ids[0]):
            raise ValueError("Partial-score case changed source ownership")
        if event["status"] not in {"COMPLETED", "UNRESOLVED"}:
            raise ValueError("Unknown case completion status")
        if event["status"] == "COMPLETED":
            matches = [v for v in verdicts if v.output_alias == alias
                       and (v.task_id == key or v.task_id.startswith(key + "::shard:"))]
            if (not matches or event["errors"] or any(v.state == "HOLD" for v in matches)
                    or event["bit"] != min(int(v.state == "PASS") for v in matches)):
                raise ValueError("Partial-score credit differs from retained scientific verdicts")


def build_score_receipt(store, receipt_ref, diagnostic_ref, scoring_policy):
    receipt = store.load(receipt_ref, SampledSilverReceipt)
    validate_policy(scoring_policy, receipt.binding.round_id)
    diagnostic = json.loads(store.read(diagnostic_ref))
    if diagnostic.get("strict_receipt") != receipt_ref.model_dump(mode="json"):
        raise ValueError("Partial-score evidence belongs to another strict receipt")
    frame = store.load(receipt.source_frame, SourceFrame)
    manifest = store.load(receipt.sampling_manifest, SampleManifest)
    events = diagnostic["events"]
    _validate_cases(store, receipt, events)
    original = project(receipt=receipt, frame=frame, manifest=manifest, events=events,
                       tolerance=diagnostic["tolerance"])
    original["strict_receipt"] = receipt_ref.model_dump(mode="json")
    if original != diagnostic:
        raise ValueError("Retained diagnostic projection does not replay")
    # Reproject the same evidence at 80%; no paid calls and no changed judgments.
    projected = project(receipt=receipt, frame=frame, manifest=manifest, events=events,
                        tolerance=policy("4/5"), exact=True)
    scores = []
    for score in projected["scores"]:
        case_events = [e for e in events if e["stage"] == "SCIENTIFIC_CASE"
                       and e.get("output_alias") == score["output_alias"]]
        resolved = [e for e in case_events if e["status"] == "COMPLETED"]
        scores.append({"output_alias": score["output_alias"], "state": "CONSERVATIVE",
            "gate": score["gate"], "score": score["exact"]["score"],
            "upper_bound": score["exact"]["upper_bound"],
            "components": {name: None if component is None else {
                **score["exact"]["components"][name],
                "resolved_units": component["resolved_units"], "planned_units": component["planned_units"],
                "coverage_target_met": component["threshold_met"]}
                for name, component in score["components"].items()},
            "coverage_target_met": score["coverage_accepted"],
            "failure_counts": {"resolved_scientific_pass": sum(e["bit"] == 1 for e in resolved),
                "resolved_scientific_fail": sum(e["bit"] == 0 for e in resolved),
                "unresolved_cases": len(case_events) - len(resolved)},
            "score_available": True})
    operations=defaultdict(lambda: {"completed":0,"unresolved":0,"attempts":0,"failed_attempts":0})
    logical=defaultdict(list);errors=Counter()
    for ref in receipt.judge_runs:
        run=store.load(ref,JudgeRunReceipt)
        operation=json.loads(store.read(run.request))["operation"]
        logical[operation,run.run_id.rsplit(":",1)[0]].append(run)
        if run.error:errors[run.error.category+":"+run.error.code]+=1
    for (name,_),runs in logical.items():
        row=operations[name]
        resolved=any(r.error is None and r.parsed_response is not None for r in runs)
        row["completed" if resolved else "unresolved"]+=1
        row["attempts"]+=len(runs);row["failed_attempts"]+=sum(r.error is not None for r in runs)
    for row in operations.values():
        row["total"]=row["completed"]+row["unresolved"]
        row["completion_fraction"]={"numerator":row["completed"],"denominator":row["total"]}
    return {"kind": KIND, "policy": scoring_policy, "strict_receipt": receipt_ref.model_dump(mode="json"),
            "diagnostic_projection": diagnostic_ref.model_dump(mode="json"),
            "strict_receipt_state": receipt.state, "scores": scores,
            "stages": projected["stages"], "failure_categories": projected["failure_categories"],
            "logical_operations": dict(operations), "failed_attempt_categories": dict(errors)}


def retain_score_receipt(store, receipt_ref, diagnostic_ref, scoring_policy):
    return store.put(build_score_receipt(store, receipt_ref, diagnostic_ref, scoring_policy))


def verify_score_receipt(store, score_ref, receipt_ref, scoring_policy):
    value = json.loads(store.read(score_ref))
    diagnostic_ref = FileRef.model_validate(value["diagnostic_projection"], strict=True)
    expected = build_score_receipt(store, receipt_ref, diagnostic_ref, scoring_policy)
    if value != expected:
        raise ValueError("First-round score differs from retained evidence or frozen policy")
    return value


def score_details(value, score_ref, alias):
    """Public completion and uncertainty metadata alongside the ordinary numeric Q."""
    row = next(s for s in value["scores"] if s["output_alias"] == alias)
    return {"evaluation_status": "CONSERVATIVE", "scoring_policy": KIND,
            "score_receipt": score_ref.model_dump(mode="json"),
            "coverage_target": "4/5", "coverage_target_met": row["coverage_target_met"],
            "gate": row["gate"], "upper_bound": row["upper_bound"],
            "components": row["components"], "failure_counts": row["failure_counts"]}
