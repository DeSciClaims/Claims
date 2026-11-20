import pytest
from miner.scientific_result_v1.table_audit import apply_layout_corrections


def test_layout_review_splits_joined_group_cells_without_changing_numbers():
    tables = [{'id': 'T1', 'columns': [{'name': 'normal', 'role': 'data'}, {'name': 'hearing_loss', 'role': 'data'}], 'annotations': []}]
    rows = {'T1': [['4.48 (3.69-5.26) 4.98 (4.39-5.56)', '']]}
    edits = [{'table_id': 'T1', 'cell_edits': [
        {'row': 1, 'column': 'normal', 'value': '4.48 (3.69-5.26)'},
        {'row': 1, 'column': 'hearing_loss', 'value': '4.98 (4.39-5.56)'}],
        'column_roles': {}, 'remove_merged_annotations': [], 'basis': 'Two group columns in source'}]
    _, fixed = apply_layout_corrections(tables, rows, edits)
    assert fixed['T1'][0] == ['4.48 (3.69-5.26)', '4.98 (4.39-5.56)']
    assert rows['T1'][0][1] == ''
    edits[0]['cell_edits'][1]['value'] = '4.99 (4.39-5.56)'
    with pytest.raises(ValueError, match='numeric source tokens'):
        apply_layout_corrections(tables, rows, edits)
