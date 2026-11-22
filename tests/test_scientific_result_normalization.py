from copy import deepcopy

from miner.scientific_result_v1.normalization import normalize_optional_absence
from miner.scientific_result_v1.validation import resolve_terminal_comparison_cores, closure_sets


def test_optional_absence_cleanup_preserves_actual_scientific_values():
    schema = {'type': 'object', 'properties': {'id': {'type': 'string'}, 'value': {},
        'notes': {'type': 'string'}, 'table_references': {'type': 'array', 'minItems': 1},
        'nullable': {'type': ['string', 'null']}}, 'required': ['id', 'value']}
    for value in [None, 0, False, [], {}, '']:
        original = {'id': 'F1', 'value': value, 'notes': None, 'table_references': [], 'nullable': None}
        clean, changes = normalize_optional_absence(original, schema)
        assert clean == {'id': 'F1', 'value': value, 'nullable': None}
        assert len(changes) == 2
        assert 'notes' in original


def test_required_absence_and_invalid_populated_data_are_not_silenced():
    schema = {'type': 'object', 'properties': {'evidence': {'type': 'array', 'minItems': 1},
        'label': {'enum': ['mean']}}, 'required': ['evidence']}
    original = {'evidence': [], 'label': 'invalid_scientific_label'}
    clean, changes = normalize_optional_absence(original, schema)
    assert clean == original
    assert not changes


def comparison():
    return {'results': [{'id': 'P1.E1.R1', 'fields': [
        {'id': 'P1.E1.R1.F1', 'label': 'mean', 'value': 1},
        {'id': 'P1.E1.R1.F2', 'label': 'mean', 'value': 2},
        {'id': 'P1.E1.R1.F3', 'label': 'p_value', 'value': .03}],
        'closure': {'core_ids': ['P1.E1.R1.F3']}}], 'relationships': [
        {'source_id': 'P1.E1.R1.F3', 'relation': 'p_value_of', 'target_id': 'P1.E1.R1', 'closure_direction': 'source_to_target'},
        *[{'source_id': 'P1.E1.R1', 'relation': 'compares', 'target_id': f'P1.E1.R1.F{i}',
           'role': role, 'closure_direction': 'target_to_source', 'basis': 'Source comparison'}
          for i, role in [(1, 'reference'), (2, 'evaluated')]]]}


def test_terminal_core_is_derived_only_from_existing_reviewed_comparison_links():
    payload = comparison()
    original = deepcopy(payload)
    audit = resolve_terminal_comparison_cores(payload)
    assert audit[0]['previous_core_ids'] == ['P1.E1.R1.F3']
    assert payload['results'][0]['closure']['core_ids'] == ['P1.E1.R1']
    assert payload['relationships'] == original['relationships']
    assert payload['results'][0]['fields'] == original['results'][0]['fields']
    members, _ = closure_sets(payload, payload['results'][0])
    assert members == {'P1.E1.R1', 'P1.E1.R1.F1', 'P1.E1.R1.F2', 'P1.E1.R1.F3'}


def test_storage_unreviewed_edges_cycles_or_nonterminal_anchors_do_not_resolve_cores():
    for mode in ['storage_only', 'unreviewed', 'outgoing', 'cycle']:
        payload = comparison()
        if mode == 'storage_only':
            payload['relationships'] = []
        elif mode == 'unreviewed':
            payload['relationships'][1].pop('closure_direction')
        elif mode == 'outgoing':
            payload['relationships'].append({'source_id': 'P1.E1.R1', 'target_id': 'P1.E1.R1.F1', 'relation': 'describes', 'closure_direction': 'source_to_target'})
        else:
            payload['relationships'].extend([{'source_id': f'P1.E1.R1.F{i}', 'target_id': f'P1.E1.R1.F{j}', 'relation': 'derived_from', 'closure_direction': 'source_to_target'} for i,j in [(1,2),(2,1)]])
        assert not resolve_terminal_comparison_cores(payload), mode
        assert payload['results'][0]['closure']['core_ids'] == ['P1.E1.R1.F3']
