"""Explicit local-only continuation policy and conservative partial-score projection.

Strict guide receipts remain HOLD when incomplete. This separate projection keeps
unknown work in its denominator and never represents evaluator failure as a proven
scientific error. No production ranking/receipt contracts are changed.
"""
from collections import Counter, defaultdict
from fractions import Fraction

from .store import canonical, sha

KIND = "claims_failure_tolerance_diagnostic_v1"


def policy(minimum="1/2", workers=20):
    result = {"kind": KIND, "minimum_resolved_fraction": minimum,
              "max_workers": workers, "unknown_credit": "zero_lower_bound",
              "competition_eligible": False}
    validate(result)
    return result


def validate(value):
    if (set(value) != {"kind", "minimum_resolved_fraction", "max_workers", "unknown_credit", "competition_eligible"}
            or value["kind"] != KIND or value["competition_eligible"] is not False
            or value["unknown_credit"] != "zero_lower_bound"
            or type(value["max_workers"]) is not int or not 1 <= value["max_workers"] <= 20):
        raise ValueError("Invalid local failure-tolerance policy")
    threshold = Fraction(value["minimum_resolved_fraction"])
    if not 0 < threshold <= 1:
        raise ValueError("Coverage threshold must be positive and no greater than one")
    return value


def failure(ops, exc, stage):
    from .operations import Hold
    from .errors import hold_record, safe_message
    if not isinstance(exc, Hold):
        exc = Hold("EVALUATOR", type(exc).__name__ + ": " + safe_message(exc, [getattr(r, "api_key", None) for r in ops.runtimes.values()]))
    return hold_record(exc, stage)


def stage_event(ops, stage, key, errors=(), **extra):
    record = {"stage": stage, "key": key, "status": "UNRESOLVED" if errors else "COMPLETED",
              "errors": [e.model_dump(mode="json") for e in errors], **extra}
    ops.continuation_events.append(record)
    return record


def mean(values):
    values = list(values)
    return sum(values, Fraction()) / len(values) if values else None


def metric(outcomes):
    if not outcomes:
        return Fraction(0), Fraction(1), Fraction(0), 0, 1
    known = [x for x in outcomes if x["status"] == "COMPLETED"]
    lo = mean(Fraction(x["bit"]) if x["status"] == "COMPLETED" else Fraction(0) for x in outcomes)
    hi = mean(Fraction(x["bit"]) if x["status"] == "COMPLETED" else Fraction(1) for x in outcomes)
    return lo, hi, Fraction(len(known), len(outcomes)), len(known), len(outcomes)


def project(*, receipt, frame, manifest, events, tolerance, exact=False):
    """Keep guide source/tile/table/bucket hierarchy, including failed task builds."""
    validate(tolerance)
    threshold = Fraction(tolerance["minimum_resolved_fraction"])
    tiles = {tile.tile_id: tile for tile in frame.tiles}
    selections = defaultdict(dict)
    for selected in manifest.selections:
        selections[selected.output_alias][selected.stream] = selected.selected_ids
    gates = {gate.output_alias: gate for gate in receipt.deterministic_gates}
    aliases = [s.output_alias for s in receipt.paper_scores]
    scores = []
    for alias in aliases:
        rows = [e for e in events if e["stage"] == "SCIENTIFIC_CASE" and e.get("output_alias") == alias]
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row["task_kind"], row["unit_id"])].append(row)
        common = selections[None]
        def source_metric(stream, kind, hierarchy):
            ids = common.get(stream, [])
            if not ids:
                return None
            groups = defaultdict(list)
            counts = [0, 0]
            for key in ids:
                values = metric(grouped[kind, key])
                counts[0] += values[3]; counts[1] += values[4]
                groups[tiles[key].table_uid if hierarchy else key].append(values[:3])
            aggregate = [mean(mean(v[i] for v in group) for group in groups.values()) for i in range(3)]
            return (*aggregate, *counts)
        cp = source_metric("CONTENT_PROSE", "CONTENT", False)
        ct = source_metric("CONTENT_TABLE", "CONTENT", True)
        available = [x for x in (cp, ct) if x is not None]
        c = (*[mean(x[i] for x in available) for i in range(3)], sum(x[3] for x in available), sum(x[4] for x in available)) if available else None
        t = source_metric("ARCHIVE", "ARCHIVE", True)
        chosen = selections[alias].get("OUTPUT_AUDIT")
        # If earlier binding/table failures prevented audit sampling, the ENTIRE P
        # component is unknown. It is not treated as an empty-assertion P=1.
        if chosen is None:
            p = metric([])
        else:
            buckets = [(key, metric(grouped["OUTPUT_AUDIT", key])) for key in chosen]
            anchored = [value for key, value in buckets if key != "UNANCHORED"]
            unanchored = next((value for key, value in buckets if key == "UNANCHORED"), None)
            if anchored:
                p = (*[mean(x[i] for x in anchored) for i in range(3)], sum(x[3] for x in anchored), sum(x[4] for x in anchored))
            else:
                p = (Fraction(1), Fraction(1), Fraction(1), 0, 0)
            if unanchored is not None:
                p = (min(p[0], unanchored[0]), min(p[1], unanchored[1]),
                     min(p[2], unanchored[2]), p[3] + unanchored[3], p[4] + unanchored[4])
        gate = gates.get(alias)
        g = gate.gate_bit if gate else None
        def q(upper):
            if g == 0:
                return Fraction(0)
            if not upper and g is None:
                return Fraction(0)
            content = (7 * c[upper] if c else 0) + (2 * t[upper] if t else 0)
            weight = (7 if c else 0) + (2 if t else 0)
            return p[upper] * content / weight if weight else Fraction(int(upper))
        def encode(value):
            if value is None:
                return None
            return {"lower": float(value[0]), "upper": float(value[1]), "resolved_weight_fraction": float(value[2]),
                    "resolved_units": value[3], "planned_units": value[4], "threshold_met": value[2] >= threshold}
        components = {"C": encode(c), "T": encode(t), "P": encode(p)}
        result = {"output_alias": alias, "gate": g, "conservative_score": float(q(False)),
                       "score_upper_bound": float(q(True)), "components": components,
                       "coverage_accepted": g is not None and all(v["threshold_met"] for v in components.values() if v is not None),
                       "package_failure": g == 0, "official_q": None}
        if exact:
            def fraction(value):
                return {"numerator": value.numerator, "denominator": value.denominator}
            result["exact"] = {"score": fraction(q(False)), "upper_bound": fraction(q(True)),
                "components": {name: None if value is None else {
                    "lower": fraction(value[0]), "upper": fraction(value[1]),
                    "resolved_weight_fraction": fraction(value[2])}
                    for name, value in (("C", c), ("T", t), ("P", p))}}
        scores.append(result)
    stages = {}
    for name in sorted({e["stage"] for e in events}):
        selected = [e for e in events if e["stage"] == name]
        done = sum(e["status"] == "COMPLETED" for e in selected)
        stages[name] = {"completed": done, "unresolved": len(selected)-done, "total": len(selected),
                        "completion_fraction": done / len(selected)}
    return {"kind": KIND, "competition_eligible": False, "tolerance": tolerance,
            "strict_receipt_state": receipt.state, "scores": scores, "stages": stages,
            "failure_categories": dict(Counter(error["category"] for e in events for error in e["errors"])),
            "events": events,
            "interpretation": "Scores are conservative diagnostic bounds. Evaluator failures receive no demonstrated credit but are not proven miner errors. Official guide Q remains null while its receipt is HOLD."}
