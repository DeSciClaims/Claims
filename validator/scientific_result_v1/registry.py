"""Every canonical guide rule is visible, including checks not yet executed."""

from .structural import load_schema

# Rules needing source interpretation in addition to mechanically executable parts.
REVIEW = {
    "attachments": {"V07", "V08", "V09", "V11", "V13", "V19", "V25"},
    "families": {"V10", "V16", "V18"},
    "coverage": {"V17", "V20", "V21", "V23", "V24", "V26", "V28", "V29", "V30"},
}
SEMANTIC = set.union(*REVIEW.values())


def rules():
    return {
        item["id"]: {**item, "requires_source_review": item["id"] in SEMANTIC}
        for item in load_schema()["x-validation_checks"]
    }


def targets(payload, category):
    if category == "families":
        ids = [g["id"] for g in payload["result_groups"]]
        return {rule: ids for rule in REVIEW[category] if rule in rules()}
    if category == "coverage":
        assigned = {
            rule: ["$extraction"] for rule in REVIEW[category] if rule in rules()
        }
        if not payload["tables"]:
            for rule in ("V21", "V23", "V24", "V26"):
                assigned[
                    rule
                ] = []  # Source table omissions are still reviewed under V20.
        availability = payload.get("data_code_availability")
        if not availability:
            for rule in ("V28", "V29", "V30"):
                assigned[
                    rule
                ] = []  # Optional extension is absent, not an assertion of unavailable data.
        elif not availability.get("artifacts"):
            assigned["V28"] = []
            assigned["V30"] = []
        return assigned
    fields = [
        f
        for key in ("results", "shared_context", "cross_group_summaries")
        for r in payload.get(key, [])
        for f in r.get("fields", [])
    ]
    ids = [f["id"] for f in fields]
    compares = [
        e["source_id"]
        for e in payload.get("relationships", [])
        if e["relation"] == "compares"
    ]
    selections = {
        "V07": list(dict.fromkeys(compares)),
        "V08": [
            f["id"]
            for f in fields
            if f["label"]
            in {
                "standard_error",
                "standard_deviation",
                "confidence_interval",
                "confidence_limit",
                "p_value",
                "count",
                "sample_size",
                "proportion",
            }
        ],
        "V09": ids,
        "V11": [
            r["id"]
            for key in ("results", "shared_context", "cross_group_summaries")
            for r in payload.get(key, [])
        ],
        "V13": [r["id"] for r in payload.get("results", [])],
        "V19": [f["id"] for f in fields if f["label"] == "equation"],
        "V25": [f["id"] for f in fields if f.get("table_references")],
    }
    return selections
