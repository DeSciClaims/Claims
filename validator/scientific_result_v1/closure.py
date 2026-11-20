"""Read-only checks of terminal components and explicit unresolved judgments."""

from collections import defaultdict
from .graph import objects, closure_sets


def checks(payload):
    issues = []
    inventory = objects(payload)
    science = {k for k, v in inventory.items() if "fields" in v or "value" in v}
    graph = defaultdict(set)
    for edge in payload.get("relationships", []):
        a, b = edge["source_id"], edge["target_id"]
        direction = edge.get("closure_direction")
        if (
            a not in science
            or b not in science
            or direction not in {"source_to_target", "target_to_source"}
        ):
            continue
        if direction == "target_to_source":
            a, b = b, a
        graph[a].add(b)
    for obj in inventory.values():
        if obj.get("parent_field_id") in science:
            graph[obj["id"]].add(obj["parent_field_id"])
    for result in payload["results"]:
        cores = set(result["closure"]["core_ids"])
        members, _ = closure_sets(payload, result)
        # Current result is an explicit possible anchor, never an inferred storage sibling.
        scope = members | {result["id"]}

        def reach(start):
            seen, todo = set(), [start]
            while todo:
                node = todo.pop()
                if node not in seen:
                    seen.add(node)
                    todo.extend((graph[node] & scope) - seen)
            return seen

        if cores and not all(cores <= reach(core) for core in cores):
            issues.append(
                {
                    "code": "V13",
                    "path": result["id"],
                    "message": "Multiple cores must share one strongly connected component; use an explicit compound anchor otherwise.",
                }
            )
        for core in cores:
            component = {node for node in reach(core) if core in reach(node)}
            if any((graph[node] & scope) - component for node in component):
                issues.append(
                    {
                        "code": "V13",
                        "path": result["id"],
                        "message": "Selected core is not terminal in the explicitly represented local graph.",
                    }
                )
                break
            if (
                (len(component) > 1 or core in graph[core])
                and not result["closure"].get("notes")
                and result["closure"]["status"] != "review_required"
            ):
                issues.append(
                    {
                        "code": "V13",
                        "path": result["id"],
                        "message": "Nontrivial cycle requires scientific explanation or review_required status.",
                    }
                )
    reviews = {r["object_id"] for r in payload.get("closure_review", [])}
    for result in payload["results"]:
        if (
            reviews & ({result["id"]} | {f["id"] for f in result["fields"]})
            and result["closure"]["status"] == "closed"
        ):
            issues.append(
                {
                    "code": "V14",
                    "path": result["id"],
                    "message": "Result contains explicitly unresolved objects but is marked closed.",
                }
            )
    return issues
