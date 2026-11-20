from miner.scientific_result_v1.table_layout import source_rows


def test_only_explicit_merged_cells_are_deduplicated():
    rows = [["group", "p"], ["a", ".001"], ["b", ".001"], ["c", ".001"]]
    data = {"num_rows": 4, "num_cols": 2, "table_cells": [{
        "start_row_offset_idx": 1, "end_row_offset_idx": 3,
        "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": ".001",
    }]}
    normalized, spans = source_rows(rows, data)
    assert normalized[1][1] == ".001"
    assert normalized[2][1] == ""
    assert normalized[3][1] == ".001"  # Equal value outside the span stays independent.
    assert rows[2][1] == ".001"
    assert spans[0]["rows"] == [1, 2]


def test_ambiguous_mapping_does_not_erase_distinct_values():
    rows = [["group", "p"], ["a", ".001"], ["b", ".05"]]
    data = {"num_rows": 3, "num_cols": 2, "table_cells": [{
        "start_row_offset_idx": 1, "end_row_offset_idx": 3,
        "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": ".001",
    }]}
    assert source_rows(rows, data) == (rows, [])
