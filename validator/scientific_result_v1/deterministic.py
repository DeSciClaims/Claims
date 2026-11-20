"""Read-only structural, reference, graph and delivered-file checks."""

from __future__ import annotations
import csv
import re
from pathlib import Path
from typing import Any
from .structural import structural_issues
from . import references, closure, tables as table_checks
from .graph import objects, definitions, walk, closure_sets, resolve_field_path


def validate_package(
    payload: Any, directory: Path, *, require_complete: bool = False
) -> list[dict]:
    issues = [{"code": "schema", **issue} for issue in structural_issues(payload)]
    if issues:
        return issues
    from .content import placeholder_issues

    issues.extend(placeholder_issues(payload))
    inventory = objects(payload)
    prefix = f"{payload['paper']['id']}.{payload['extraction']['id']}."

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
        for key in (
            "target_id",
            "parent_field_id",
            "object_id",
            "group_id",
            "table_id",
        ):
            if key in node and node[key] not in inventory:
                issue("V02", node.get("id", key), f"Unresolved {key}: {node[key]}")
        if (
            "locator" in node
            and "text" in node
            and node.get("source_id") not in source_ids
        ):
            issue("V02", "source_snippets", "Snippet source is not a defined source")
        for key in (
            "core_ids",
            "member_ids",
            "reference_ids",
            "related_ids",
            "related_group_ids",
            "group_ids",
            "reviewed_source_ids",
        ):
            for ref in node.get(key, []):
                if ref not in inventory:
                    issue("V02", key, f"Unresolved reference: {ref}")
                if key in ("core_ids", "member_ids") and not re.search(
                    r"\.[RC]\d+(?:\.F\d+)?$", ref
                ):
                    issue("V27", key, f"Ineligible closure object: {ref}")
                if key == "reviewed_source_ids" and ref not in source_ids:
                    issue("V02", key, "Reviewed source is not a source")
        if node.get("parent_field_id") in inventory:
            parent = inventory[node["parent_field_id"]]
            try:
                if (
                    resolve_field_path(parent["value"], node["field_path"])
                    != node["value"]
                ):
                    issue(
                        "V05",
                        node["id"],
                        "Addressed value differs from parent component",
                    )
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
        owned = {
            key for key, record in results.items() if record["group_id"] == group_id
        }
        if not owned or not set(group["core_result_ids"]) <= owned:
            issue(
                "V03", group_id, "Family needs results and core results belonging to it"
            )
    for result in results.values():
        if result["group_id"] not in groups:
            issue("V03", result["id"], "Primary family is not a defined result group")
    for summary in payload.get("cross_group_summaries", []):
        if any(key not in groups for key in summary["group_ids"]):
            issue(
                "V03",
                summary["id"],
                "Cross-group summary references an undefined family",
            )
    for edge in payload.get("relationships", []):
        if edge["source_id"] not in inventory or edge["target_id"] not in inventory:
            issue("V02", "relationships", "Unresolved relationship endpoint")
        if edge["relation"] == "table_for" and (
            edge["source_id"] not in tables or edge["target_id"] not in results
        ):
            issue(
                "V24",
                "relationships",
                "table_for must link a table to an ordinary result",
            )
    for result in results.values():
        members, refs = closure_sets(payload, result)
        if members != set(result["closure"]["member_ids"]):
            issue(
                "V12",
                result["id"],
                "member_ids differs from computed reverse reachability",
            )
        if refs != set(result["closure"]["reference_ids"]):
            issue("V15", result["id"], "reference_ids differs from computed boundary")
        if result["closure"]["status"] == "closed" and any(
            "closure_direction" not in edge
            and (edge["source_id"] in members or edge["target_id"] in members)
            for edge in payload.get("relationships", [])
        ):
            issue(
                "V14", result["id"], "Closed result has unreviewed incident direction"
            )
    accounted = set()
    for result in results.values():
        accounted.update(result["closure"]["member_ids"])
        accounted.update(result["closure"]["reference_ids"])
    accounted.update(item["object_id"] for item in payload.get("closure_review", []))
    for result in results.values():
        for field in result["fields"]:
            if field["id"] not in accounted:
                issues.append(
                    {
                        "code": "V17",
                        "path": field["id"],
                        "severity": "review_required",
                        "message": "Field is not a closure member, boundary reference or explicit review item; source review must establish contextual status or report an omission.",
                    }
                )
    for table in tables.values():
        key = table["id"]
        if table["source_id"] not in source_ids:
            issue("V02", key, "Table source is not a defined source")
        if table["csv_file"] != key + ".csv":
            issue("V21", key, "csv_file must equal table ID + .csv")
            continue
        if not any(
            edge["relation"] == "table_for" and edge["source_id"] == key
            for edge in payload.get("relationships", [])
        ):
            issue("V24", key, "Table has no table_for relationship")
        columns = [column["name"] for column in table["columns"]]
        rows = None
        try:
            path = directory / table["csv_file"]
            if path.is_symlink() or path.resolve().parent != directory.resolve():
                raise ValueError("CSV must be a regular package file without symlinks")
            data = path.read_bytes()
            if data.startswith(b"\xef\xbb\xbf"):
                raise ValueError("UTF-8 BOM is not permitted")
            from .tables import check_csv_wire

            check_csv_wire(data.decode("utf-8"))
            rows = list(
                csv.reader(data.decode("utf-8").splitlines(keepends=True), strict=True)
            )
            if not rows or rows[0] != columns or len(set(columns)) != len(columns):
                raise ValueError("CSV header must exactly match unique columns[].name")
            if len(rows) - 1 != table["row_count"] or any(
                len(row) != len(columns) for row in rows[1:]
            ):
                raise ValueError("CSV row count or width differs from table metadata")
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            issue("V22", key, str(exc))
        for annotation in table["annotations"]:
            if any(
                row > table["row_count"] for row in annotation.get("rows", [])
            ) or any(column not in columns for column in annotation.get("columns", [])):
                issue("V26", key, "Annotation row or column is outside table")
            elif rows and annotation["kind"] == "merged_cell":
                row_ids = sorted(annotation["rows"])
                column_ids = sorted(
                    columns.index(column) for column in annotation["columns"]
                )
                for row in row_ids:
                    for column in column_ids:
                        if (
                            row < len(rows)
                            and column < len(rows[row])
                            and (row, column) != (row_ids[0], column_ids[0])
                            and rows[row][column].strip()
                        ):
                            issue(
                                "V26",
                                key,
                                "Merged source cell must appear once at its first covered row and column",
                            )
    for node in inventory.values():
        for ref in node.get("table_references", []):
            table = tables.get(ref["table_id"])
            if table and (
                ref["row"] > table["row_count"]
                or ref["column"] not in [column["name"] for column in table["columns"]]
            ):
                issue("V25", node["id"], "Table cell coordinate is outside table")
    artifacts = payload.get("data_code_availability", {}).get("artifacts", [])
    artifact_links = set()
    for artifact in artifacts:
        texts = [snippet["text"] for snippet in artifact["source_snippets"]]
        for name in ("url", "identifier"):
            if name in artifact:
                if not any(artifact[name] in text for text in texts):
                    issue(
                        "V28",
                        artifact["id"],
                        f"{name} is absent from its source snippets",
                    )
                if (name, artifact[name]) in artifact_links:
                    issue("V30", artifact["id"], "Duplicate artifact link")
                artifact_links.add((name, artifact[name]))
    if (
        require_complete
        and payload["extraction"]["table_coverage"]["status"] != "complete"
    ):
        issue(
            "V20",
            "extraction.table_coverage",
            "Incomplete extraction is not a completed submission",
        )
    if require_complete:
        for result in results.values():
            if result["closure"]["status"] != "closed":
                issue(
                    "V14",
                    result["id"],
                    "Unresolved local closure is not a completed submission",
                )
    issues.extend(references.checks(payload))
    issues.extend(closure.checks(payload))
    issues.extend(table_checks.checks(payload, directory))
    return issues
