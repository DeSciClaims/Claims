"""Type-aware reference and comparison-orientation invariants."""

from .graph import definitions, objects


def checks(payload):
    issues = []
    inventory = objects(payload)
    types = {}
    for section, kind in [
        ("sources", "source"),
        ("tables", "table"),
        ("result_groups", "group"),
        ("results", "result"),
        ("shared_context", "context"),
        ("cross_group_summaries", "summary"),
    ]:
        for obj in payload.get(section, []):
            types[obj["id"]] = kind
            for field in obj.get("fields", []):
                types[field["id"]] = "field"

    def require(key, kind, owner):
        if key in inventory and types.get(key) != kind:
            issues.append(
                {
                    "code": "V02",
                    "path": owner,
                    "message": f"{key} must reference a {kind}.",
                }
            )

    for obj in definitions(payload):
        if "parent_field_id" in obj:
            require(obj["parent_field_id"], "field", obj["id"])
        if "group_id" in obj:
            require(obj["group_id"], "group", obj["id"])
        for group in obj.get("group_ids", []):
            require(group, "group", obj["id"])
    for edge in payload.get("relationships", []):
        science = {"field", "result", "context", "summary"}
        endpoint_types = {types.get(edge["source_id"]), types.get(edge["target_id"])}
        if (
            edge["relation"] in {"uncertainty_of", "spread_of", "compares", "tests"}
            and not endpoint_types <= science
        ):
            issues.append(
                {
                    "code": "V02",
                    "path": edge["source_id"],
                    "message": "Scientific test/comparison/uncertainty endpoints must resolve to scientific objects, not metadata or source containers.",
                }
            )
        if (
            edge.get("closure_direction") in {"source_to_target", "target_to_source"}
            and not endpoint_types <= science
        ):
            issues.append(
                {
                    "code": "V27" if "table" in endpoint_types else "V13",
                    "path": edge["source_id"],
                    "message": "Metadata/source-container edges cannot project scientific closure dependencies.",
                }
            )
        if edge["relation"] == "compares":
            if edge.get("role") not in {"evaluated", "reference"}:
                issues.append(
                    {
                        "code": "V07",
                        "path": edge["source_id"],
                        "message": "Comparison needs an evaluated/reference role; raw endpoint order is insufficient.",
                    }
                )
        if edge["relation"] in {"uncertainty_of", "spread_of"}:
            source, target = (
                inventory.get(edge["source_id"], {}),
                inventory.get(edge["target_id"], {}),
            )
            if target.get("label") in {
                "standard_error",
                "standard_deviation",
                "confidence_interval",
                "confidence_limit",
            } and source.get("label") in {
                "mean",
                "treatment_effect",
                "odds_ratio",
                "association",
            }:
                issues.append(
                    {
                        "code": "V08",
                        "path": edge["source_id"],
                        "message": "Uncertainty/spread relationship is reversed: uncertainty is source, estimate is target.",
                    }
                )
    return issues
