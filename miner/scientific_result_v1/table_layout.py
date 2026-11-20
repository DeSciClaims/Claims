"""Remove dataframe duplication only where Docling explicitly reports a merged cell."""
from __future__ import annotations

from copy import deepcopy
import re


def source_rows(rows: list[list[str]], data: dict) -> tuple[list[list[str]], list[dict]]:
    """Return data with shared source cells stored once, plus raw CSV span metadata.

    Never infer merging from equal values. Only apply explicit cell geometry when
    dataframe dimensions and every covered display value agree with the source cell.
    Row numbers in the metadata exclude the CSV header and begin at one.
    """
    output = deepcopy(rows)
    spans = []
    if not rows or len(rows[0]) != data.get("num_cols"):
        return output, spans
    header_count = data.get("num_rows", 0) - (len(rows) - 1)
    if header_count < 0:
        return output, spans
    normalize = lambda text: re.sub(r"\s+", " ", text.strip())
    for cell in data.get("table_cells", []):
        start = cell["start_row_offset_idx"]
        end = cell["end_row_offset_idx"]
        left = cell["start_col_offset_idx"]
        right = cell["end_col_offset_idx"]
        if start < header_count or (end - start == 1 and right - left == 1):
            continue
        row_ids = list(range(start - header_count + 1, end - header_count + 1))
        column_ids = list(range(left, right))
        if not row_ids or not column_ids or any(row < 1 or row >= len(rows) for row in row_ids):
            continue
        if any(column < 0 or column >= len(rows[0]) for column in column_ids):
            continue
        expected = normalize(cell.get("text", ""))
        if not expected or any(normalize(rows[row][column]) not in ("", expected)
                               for row in row_ids for column in column_ids):
            continue
        for row in row_ids:
            for column in column_ids:
                output[row][column] = cell["text"] if row == row_ids[0] and column == column_ids[0] else ""
        spans.append({"rows": row_ids, "column_indexes": column_ids,
                      "column_headers": [rows[0][column] for column in column_ids],
                      "text": cell["text"], "basis": "Explicit Docling merged-cell geometry; not independent repeated observations"})
    return output, spans
