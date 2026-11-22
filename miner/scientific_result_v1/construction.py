"""Explicit miner-side graph construction; never called by authoritative validation."""
from collections import defaultdict
import re
from validator.scientific_result_v1.graph import objects, closure_sets

def resolve_terminal_comparison_cores(payload: dict) -> list[dict]:
    """Project unambiguous terminal comparison anchors from reviewed scientific links.

    No storage/group membership is used and no edges or values are created. A
    comparison must already have explicit evaluated/reference input links, all
    selected cores must reach it, and the anchor must be terminal in the graph.
    """
    inventory = objects(payload)
    successors = defaultdict(set)
    for edge in payload.get("relationships", []):
        source, target = edge["source_id"], edge["target_id"]
        if edge.get("closure_direction") == "target_to_source":
            source, target = target, source
        elif edge.get("closure_direction") != "source_to_target":
            continue
        if source in inventory and target in inventory and not any(
            re.search(r"\.[GSTA]\d+$", key) for key in (source, target)
        ):
            successors[source].add(target)
    for item in inventory.values():
        if item.get("parent_field_id") in inventory:
            successors[item["id"]].add(item["parent_field_id"])
    def reaches(start, destination):
        pending, visited = [start], set()
        while pending:
            node = pending.pop()
            if node == destination:
                return True
            if node not in visited:
                visited.add(node)
                pending.extend(successors[node] - visited)
        return False
    resolutions = []
    for result in payload.get("results", []):
        anchor = result["id"]
        cores = result["closure"]["core_ids"]
        if cores == [anchor] or not cores or successors[anchor]:
            continue
        links = [edge for edge in payload.get("relationships", [])
                 if edge["relation"] == "compares" and edge["source_id"] == anchor
                 and edge.get("closure_direction") == "target_to_source"
                 and edge["target_id"] in inventory
                 and not re.search(r"\.[GSTA]\d+$", edge["target_id"])
                 and (edge.get("basis") or edge.get("source_snippets"))]
        if (len({edge["target_id"] for edge in links}) < 2
                or not {'evaluated', 'reference'} <= {edge.get('role') for edge in links}
                or not all(reaches(core, anchor) for core in cores)):
            continue
        candidate = {**result, "closure": {**result["closure"], "core_ids": [anchor]}}
        ancestors, _ = closure_sets(payload, candidate)
        if not ancestors <= inventory.keys():
            continue
        colors = {}
        def cyclic(node):
            if colors.get(node) == 1:
                return True
            if colors.get(node) == 2:
                return False
            colors[node] = 1
            if any(cyclic(child) for child in successors[node] & ancestors):
                return True
            colors[node] = 2
            return False
        if any(cyclic(node) for node in ancestors):
            continue
        resolutions.append({'result_id': anchor, 'previous_core_ids': list(cores),
                            'terminal_core_ids': [anchor],
                            'basis': 'Existing reviewed evaluated/reference comparison links; every prior core reaches this terminal anchor.'})
        result["closure"]["core_ids"] = [anchor]
        note = 'Graph projection selected the terminal comparison anchor from existing reviewed scientific links; no edges or values were added.'
        result["closure"]["notes"] = (result["closure"].get("notes", '') + ' ' + note).strip()
    return resolutions


def recompute_closures(payload: dict) -> None:
    """Compute membership after core/direction review; never infer graph from groups."""
    for result in payload.get("results", []):
        members, refs = closure_sets(payload, result)
        result["closure"]["member_ids"] = sorted(members)
        result["closure"]["reference_ids"] = sorted(refs)
        if any(
            "closure_direction" not in edge
            and (edge["source_id"] in members or edge["target_id"] in members)
            for edge in payload.get("relationships", [])
        ):
            result["closure"].update(status="review_required", notes="Incident relationship direction is unreviewed.")
