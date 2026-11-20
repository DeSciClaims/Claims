"""Pure identity, path and reviewed-graph projections."""

from __future__ import annotations
import json
import re
from collections import defaultdict
from typing import Any


def definitions(payload: dict) -> list[dict]:
    records = [
        obj
        for name in (
            "sources",
            "tables",
            "result_groups",
            "results",
            "shared_context",
            "cross_group_summaries",
        )
        for obj in payload.get(name, [])
    ]
    records += [
        field
        for name in ("results", "shared_context", "cross_group_summaries")
        for record in payload.get(name, [])
        for field in record.get("fields", [])
    ]
    records += payload.get("data_code_availability", {}).get("artifacts", [])
    return records


def objects(payload: dict) -> dict[str, dict]:
    return {obj["id"]: obj for obj in definitions(payload)}


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for key, item in value.items():
            if key not in {"value", "context"}:
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
