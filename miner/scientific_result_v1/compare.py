"""ID-independent diagnostics against worked examples; not a semantic accuracy score."""
from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


def fields(payload: dict):
    return [field for key in ("results", "shared_context", "cross_group_summaries")
            for record in payload.get(key, []) for field in record.get("fields", [])]


def _leaves(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _leaves(child)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield format(value, ".12g")


def _cells(directory: Path, payload: dict) -> Counter:
    cells = Counter()
    for table in payload.get("tables", []):
        # Never read arbitrary paths from an extraction during evaluation.
        name = table["csv_file"]
        if Path(name).name != name:
            continue
        path = directory / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        cells.update(re.sub(r"\s+", " ", cell.strip()) for row in rows[1:] for cell in row if cell.strip())
    return cells


def _data_cells(directory: Path, payload: dict) -> Counter:
    """Compare display data after Unicode typography normalization, without IDs."""
    cells = Counter()
    for table in payload.get("tables", []):
        name = table["csv_file"]
        if Path(name).name != name or not (directory / name).is_file():
            continue
        with (directory / name).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        data_indexes = [index for index, column in enumerate(table["columns"]) if column["role"] == "data"]
        for row in rows[1:]:
            for index in data_indexes:
                if index < len(row) and row[index].strip():
                    value = unicodedata.normalize("NFKC", row[index]).replace("−", "-").replace("–", "-")
                    cells[re.sub(r"\s+", " ", value.strip())] += 1
    return cells


def _data_rows(directory: Path, table: dict) -> list[list[str]] | None:
    name = table["csv_file"]
    if Path(name).name != name or not (directory / name).is_file():
        return None
    with (directory / name).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    indexes = [index for index, column in enumerate(table["columns"]) if column["role"] == "data"]
    result = []
    for row in rows[1:]:
        values = [re.sub(r"\s+", " ", unicodedata.normalize("NFKC", row[index]).replace("−", "-").replace("–", "-").strip())
                  for index in indexes if index < len(row)]
        if any(values):
            result.append(values)
    return result


def compare(actual_path: Path, expected_path: Path) -> dict:
    actual = json.loads(actual_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    actual_fields, expected_fields = fields(actual), fields(expected)
    a_numbers = Counter(number for field in actual_fields for number in _leaves(field["value"]))
    e_numbers = Counter(number for field in expected_fields for number in _leaves(field["value"]))
    a_reported = Counter(number for field in actual_fields if "digitiz" not in field.get("origin", "").lower() for number in _leaves(field["value"]))
    e_reported = Counter(number for field in expected_fields if "digitiz" not in field.get("origin", "").lower() for number in _leaves(field["value"]))
    a_cells, e_cells = _cells(actual_path.parent, actual), _cells(expected_path.parent, expected)
    a_data, e_data = _data_cells(actual_path.parent, actual), _data_cells(expected_path.parent, expected)
    return {
        "actual": str(actual_path), "expected": str(expected_path),
        "counts": {key: {"actual": len(actual.get(key, [])), "expected": len(expected.get(key, []))}
                   for key in ("results", "result_groups", "shared_context", "tables", "relationships", "cross_group_summaries")},
        "fields": {"actual": len(actual_fields), "expected": len(expected_fields)},
        "field_labels": {"actual": dict(Counter(field["label"] for field in actual_fields)),
                         "expected": dict(Counter(field["label"] for field in expected_fields))},
        "numeric_leaf_overlap": {"matched": sum((a_numbers & e_numbers).values()),
            "actual": sum(a_numbers.values()), "expected": sum(e_numbers.values())},
        "numeric_leaf_overlap_excluding_digitized": {"matched": sum((a_reported & e_reported).values()),
            "actual": sum(a_reported.values()), "expected": sum(e_reported.values())},
        "table_nonempty_cell_overlap": {"matched": sum((a_cells & e_cells).values()),
            "actual": sum(a_cells.values()), "expected": sum(e_cells.values())},
        "table_data_cell_overlap_typography_normalized": {"matched": sum((a_data & e_data).values()),
            "actual": sum(a_data.values()), "expected": sum(e_data.values())},
        "table_data_cell_differences": {
            "expected_only": dict(e_data - a_data), "actual_only": dict(a_data - e_data)},
        "ordered_table_data_comparison": [{
            "actual_table": actual_table["id"], "expected_table": expected_table["id"],
            "equal_after_typography_and_empty_layout_row_normalization":
                _data_rows(actual_path.parent, actual_table) == _data_rows(expected_path.parent, expected_table),
        } for actual_table, expected_table in zip(actual.get("tables", []), expected.get("tables", []))],
        "field_origins": {"actual": dict(Counter(field.get("origin", "unspecified") for field in actual_fields)),
                          "expected": dict(Counter(field.get("origin", "unspecified") for field in expected_fields))},
        "result_titles": {"actual": [result["title"] for result in actual.get("results", [])],
                          "expected": [result["title"] for result in expected.get("results", [])]},
        "table_rows": {"actual": [table["row_count"] for table in actual.get("tables", [])],
                       "expected": [table["row_count"] for table in expected.get("tables", [])]},
        "table_coverage": {"actual": actual["extraction"]["table_coverage"], "expected": expected["extraction"]["table_coverage"]},
        "limitations": ["Counts and value overlap are diagnostics, not semantic correctness or recall.",
                        "Equal values may describe different outcomes; representation and digitization may differ.",
                        "Source versions, supplementary scope, attachments, and precision require review."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(compare(args.actual, args.expected), ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
