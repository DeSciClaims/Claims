"""Additional delivered-cell checks; source fidelity remains a separate review."""

import csv
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from .graph import definitions


def numbers(value):
    text = unicodedata.normalize("NFKC", str(value)).replace("−", "-")
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    # A display range separator is not the upper bound's minus sign.
    text = re.sub(r"(?<=\d)\s*[-–]\s*(?=\d)", " ", text)
    return {
        Decimal(s)
        for s in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text)
    }


def checks(payload, directory):
    issues = []
    tables = {t["id"]: t for t in payload["tables"]}
    rows = {}
    for key, table in tables.items():
        path = directory / table["csv_file"]
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            issues.append(
                {
                    "code": "V22",
                    "path": key,
                    "message": "CSV must be a regular local package file, not a symlink or escaped path.",
                }
            )
            continue
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                rows[key] = list(csv.reader(handle, strict=True))
        except (OSError, UnicodeError, csv.Error):
            continue  # Primary file checks report the exact failure.
    for obj in definitions(payload):
        for ref in obj.get("table_references", []):
            table = tables.get(ref["table_id"])
            if not table or ref["table_id"] not in rows:
                continue
            headers = [c["name"] for c in table["columns"]]
            if ref["column"] not in headers or not 1 <= ref["row"] < len(
                rows[ref["table_id"]]
            ):
                continue
            data_row = rows[ref["table_id"]][ref["row"]]
            column = headers.index(ref["column"])
            if column >= len(data_row):
                continue
            value = obj.get("value")
            if type(value) in (float, int) and not any(
                word in obj.get("origin", "").lower()
                for word in ("calculat", "derived")
            ):
                try:
                    matches = Decimal(str(value)) in numbers(data_row[column])
                except InvalidOperation:
                    matches = False
                if not matches:
                    issues.append(
                        {
                            "code": "V25",
                            "path": obj["id"],
                            "severity": "fail"
                            if re.fullmatch(
                                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
                                data_row[column].strip(),
                            )
                            else "review_required",
                            "message": "Field number is not a literal number in the referenced cell. Verify sign, unit conversion, component and provenance against source.",
                        }
                    )
    return issues


def check_csv_wire(text):
    """Enforce quoting, LF record separators and final separator, including embedded newlines."""
    if not text.endswith("\n"):
        raise ValueError("CSV requires a final LF record separator")
    state = "start"
    for char in text:
        if state == "quoted":
            if char == '"':
                state = "after_quote"
        elif state == "after_quote":
            if char == '"':
                state = "quoted"
            elif char == ",":
                state = "start"
            elif char == "\n":
                state = "start"
            else:
                raise ValueError("Characters after a closing quote are invalid CSV")
        else:
            if char == "\r":
                raise ValueError(
                    "CSV record separators must be LF; CR is permitted only inside quoted cells"
                )
            if char == '"':
                if state != "start":
                    raise ValueError(
                        "A quote inside an unquoted cell must be escaped and the cell quoted"
                    )
                state = "quoted"
            elif char in {",", "\n"}:
                state = "start"
            else:
                state = "unquoted"
    if state == "quoted":
        raise ValueError("Unterminated quoted CSV cell")
