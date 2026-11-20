"""Apply bounded JSON-pointer edits to in-memory extraction data, never files."""
from __future__ import annotations

from copy import deepcopy


def apply_edits(payload: dict, edits: list[dict]) -> dict:
    result = deepcopy(payload)
    for edit in edits:
        operation, pointer = edit["op"], edit["path"]
        if operation not in ("add", "replace", "remove") or not pointer.startswith("/"):
            raise ValueError("Invalid edit operation or JSON pointer")
        tokens = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
        if tokens[0] in ("paper", "sources", "tables", "schema_version") or tokens[:2] == ["extraction", "id"]:
            raise ValueError("Repair cannot change source, table, or assigned identities")
        node = result
        for key in tokens[:-1]:
            node = node[int(key)] if isinstance(node, list) else node[key]
        key = tokens[-1]
        if isinstance(node, list):
            index = len(node) if key == "-" and operation == "add" else int(key)
            if index < 0 or index > len(node) or (operation != "add" and index == len(node)):
                raise ValueError("Edit index is outside array")
            if operation == "add":
                node.insert(index, edit["value"])
            elif operation == "replace":
                node[index] = edit["value"]
            else:
                del node[index]
        elif isinstance(node, dict):
            if operation != "add" and key not in node:
                raise ValueError(f"Edit key does not exist at {pointer}; {operation} requires an existing property. Use add to create an absent property.")
            if operation == "remove":
                del node[key]
            else:
                node[key] = edit["value"]
        else:
            raise ValueError("Edit parent is not a container")
    return result
