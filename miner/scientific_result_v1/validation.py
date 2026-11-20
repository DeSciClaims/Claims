"""Deterministic v1.1 graph, identity, reference, and delivered-file checks.

Scientific correctness and source completeness still require source review.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .schema import structural_issues


def definitions(payload: dict) -> list[dict]:
    records = [obj for name in ("sources", "tables", "result_groups", "results", "shared_context", "cross_group_summaries") for obj in payload.get(name, [])]
    records += [field for name in ("results", "shared_context", "cross_group_summaries") for record in payload.get(name, []) for field in record.get("fields", [])]
    records += payload.get("data_code_availability", {}).get("artifacts", [])
    return records


def objects(payload: dict) -> dict[str, dict]:
    return {obj["id"]: obj for obj in definitions(payload)}


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def closure_sets(payload: dict, result: dict) -> tuple[set[str], set[str]]:
    inventory = objects(payload)
    parents = defaultdict(set)
    for edge in payload.get("relationships", []):
        source, target = edge["source_id"], edge["target_id"]
        if edge.get("closure_direction") == "target_to_source":
            source, target = target, source
        elif edge.get("closure_direction") != "source_to_target":
            continue
        if re.search(r"\.[GSTA]\d+$", source) or re.search(r"\.[GSTA]\d+$", target):
            continue
        parents[target].add(source)
    for item in inventory.values():
        if item.get("parent_field_id"):
            parents[item["parent_field_id"]].add(item["id"])
    members = set(result["closure"]["core_ids"])
    pending = list(members)
    while pending:
        for child in parents[pending.pop()] - members:
            members.add(child)
            pending.append(child)
    refs = set()
    for edge in payload.get("relationships", []):
        a, b = edge["source_id"], edge["target_id"]
        if edge.get("closure_direction") == "none":
            if a in members:
                refs.add(b)
            if b in members:
                refs.add(a)
        if edge["relation"] == "table_for" and b == result["id"]:
            refs.add(a)
    for item in [result, *(inventory[key] for key in members if key in inventory)]:
        refs.update(ref["target_id"] for ref in item.get("references", []))
        refs.update(ref["table_id"] for ref in item.get("table_references", []))
    return members, refs - members


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


def resolve_field_path(value: Any, path: str) -> Any:
    # Full tokenization; no eval, ignored suffixes, or negative indexes.
    token = re.compile(r'(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)|\[(\d+|"(?:[^"\\]|\\.)*")\]')
    position = 0
    for match in token.finditer(path):
        if match.start() != position:
            raise ValueError("Invalid field path")
        key = match.group(1)
        if key is None:
            raw = match.group(2)
            key = json.loads(raw) if raw.startswith('"') else int(raw)
        value = value[key]
        position = match.end()
    if position != len(path) or not position:
        raise ValueError("Invalid field path")
    return value


def validate_package(payload: Any, directory: Path, *, require_complete: bool = False) -> list[dict]:
    issues = [{"code": "schema", **issue} for issue in structural_issues(payload)]
    if issues:
        return issues
    inventory = objects(payload)
    prefix = f'{payload["paper"]["id"]}.{payload["extraction"]["id"]}.'

    def issue(code, path, message):
        issues.append({"code": code, "path": path, "message": message})

    seen = set()
    for node in definitions(payload):
        key = node["id"]
        if key in seen:
            issue("V01", key, "ID has multiple definitions")
        seen.add(key)
        if not key.startswith(prefix):
            issue("V01", key, f"ID must have prefix {prefix}")
    source_ids = {source["id"] for source in payload["sources"]}
    groups = {group["id"]: group for group in payload["result_groups"]}
    results = {record["id"]: record for record in payload["results"]}
    tables = {table["id"]: table for table in payload["tables"]}
    for node in walk(payload):
        for key in ("target_id", "parent_field_id", "object_id", "group_id", "table_id"):
            if key in node and node[key] not in inventory:
                issue("V02", node.get("id", key), f"Unresolved {key}: {node[key]}")
        if "locator" in node and "text" in node and node.get("source_id") not in source_ids:
            issue("V02", "source_snippets", "Snippet source is not a defined source")
        for key in ("core_ids", "member_ids", "reference_ids", "related_ids", "related_group_ids", "group_ids", "reviewed_source_ids"):
            for ref in node.get(key, []):
                if ref not in inventory:
                    issue("V02", key, f"Unresolved reference: {ref}")
                if key in ("core_ids", "member_ids") and not re.search(r'\.[RC]\d+(?:\.F\d+)?$', ref):
                    issue("V27", key, f"Ineligible closure object: {ref}")
                if key == "reviewed_source_ids" and ref not in source_ids:
                    issue("V02", key, "Reviewed source is not a source")
        if node.get("parent_field_id") in inventory:
            parent = inventory[node["parent_field_id"]]
            try:
                if resolve_field_path(parent["value"], node["field_path"]) != node["value"]:
                    issue("V05", node["id"], "Addressed value differs from parent component")
            except (KeyError, IndexError, TypeError, ValueError):
                issue("V05", node["id"], "Invalid parent field or component path")
            visited = {node["id"]}
            while parent.get("parent_field_id") in inventory:
                if parent["id"] in visited:
                    issue("V05", node["id"], "Cyclic parent-field links")
                    break
                visited.add(parent["id"])
                parent = inventory[parent["parent_field_id"]]
    for group_id, group in groups.items():
        owned = {key for key, record in results.items() if record["group_id"] == group_id}
        if not owned or not set(group["core_result_ids"]) <= owned:
            issue("V03", group_id, "Family needs results and core results belonging to it")
    for result in results.values():
        if result["group_id"] not in groups:
            issue("V03", result["id"], "Primary family is not a defined result group")
    for summary in payload.get("cross_group_summaries", []):
        if any(key not in groups for key in summary["group_ids"]):
            issue("V03", summary["id"], "Cross-group summary references an undefined family")
    for edge in payload.get("relationships", []):
        if edge["source_id"] not in inventory or edge["target_id"] not in inventory:
            issue("V02", "relationships", "Unresolved relationship endpoint")
        if edge["relation"] == "table_for" and (edge["source_id"] not in tables or edge["target_id"] not in results):
            issue("V24", "relationships", "table_for must link a table to an ordinary result")
    for result in results.values():
        members, refs = closure_sets(payload, result)
        if members != set(result["closure"]["member_ids"]):
            issue("V12", result["id"], "member_ids differs from computed reverse reachability")
        if refs != set(result["closure"]["reference_ids"]):
            issue("V15", result["id"], "reference_ids differs from computed boundary")
        if result["closure"]["status"] == "closed" and any(
            "closure_direction" not in edge and (edge["source_id"] in members or edge["target_id"] in members)
            for edge in payload.get("relationships", [])
        ):
            issue("V14", result["id"], "Closed result has unreviewed incident direction")
    accounted = set()
    for result in results.values():
        accounted.update(result["closure"]["member_ids"])
        accounted.update(result["closure"]["reference_ids"])
    accounted.update(item["object_id"] for item in payload.get("closure_review", []))
    for result in results.values():
        for field in result["fields"]:
            if field["id"] not in accounted:
                issue("V17", field["id"], "Scientific field is neither a closure member, boundary reference, nor closure_review item")
    for table in tables.values():
        key = table["id"]
        if table["source_id"] not in source_ids:
            issue("V02", key, "Table source is not a defined source")
        if table["csv_file"] != key + ".csv":
            issue("V21", key, "csv_file must equal table ID + .csv")
            continue
        if not any(edge["relation"] == "table_for" and edge["source_id"] == key for edge in payload.get("relationships", [])):
            issue("V24", key, "Table has no table_for relationship")
        columns = [column["name"] for column in table["columns"]]
        rows = None
        try:
            data = (directory / table["csv_file"]).read_bytes()
            if data.startswith(b'\xef\xbb\xbf'):
                raise ValueError("UTF-8 BOM is not permitted")
            rows = list(csv.reader(data.decode("utf-8").splitlines(keepends=True), strict=True))
            if not rows or rows[0] != columns or len(set(columns)) != len(columns):
                raise ValueError("CSV header must exactly match unique columns[].name")
            if len(rows) - 1 != table["row_count"] or any(len(row) != len(columns) for row in rows[1:]):
                raise ValueError("CSV row count or width differs from table metadata")
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            issue("V22", key, str(exc))
        for annotation in table["annotations"]:
            if any(row > table["row_count"] for row in annotation.get("rows", [])) or any(column not in columns for column in annotation.get("columns", [])):
                issue("V26", key, "Annotation row or column is outside table")
            elif rows and annotation["kind"] == "merged_cell":
                row_ids = sorted(annotation["rows"])
                column_ids = sorted(columns.index(column) for column in annotation["columns"])
                for row in row_ids:
                    for column in column_ids:
                        if row < len(rows) and column < len(rows[row]) and (row, column) != (row_ids[0], column_ids[0]) and rows[row][column].strip():
                            issue("V26", key, "Merged source cell must appear once at its first covered row and column")
    for node in inventory.values():
        for ref in node.get("table_references", []):
            table = tables.get(ref["table_id"])
            if table and (ref["row"] > table["row_count"] or ref["column"] not in [column["name"] for column in table["columns"]]):
                issue("V25", node["id"], "Table cell coordinate is outside table")
    artifacts = payload.get("data_code_availability", {}).get("artifacts", [])
    artifact_links = set()
    for artifact in artifacts:
        texts = [snippet["text"] for snippet in artifact["source_snippets"]]
        for name in ("url", "identifier"):
            if name in artifact:
                if not any(artifact[name] in text for text in texts):
                    issue("V28", artifact["id"], f"{name} is absent from its source snippets")
                if (name, artifact[name]) in artifact_links:
                    issue("V30", artifact["id"], "Duplicate artifact link")
                artifact_links.add((name, artifact[name]))
    if require_complete and payload["extraction"]["table_coverage"]["status"] != "complete":
        issue("V20", "extraction.table_coverage", "Incomplete extraction is not a completed submission")
    if require_complete:
        for result in results.values():
            if result["closure"]["status"] != "closed":
                issue("V14", result["id"], "Unresolved local closure is not a completed submission")
    return issues
