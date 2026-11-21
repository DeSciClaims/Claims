"""Focused source-grounded relationship review without rewriting scientific values."""
from copy import deepcopy
import json
import re

from .schema import load_schema
from .validation import objects, recompute_closures, resolve_terminal_comparison_cores


def review_batch_graph(batch, *, context, documents, runtime, directory, stage, attempts):
    from .pipeline import SYSTEM, _call, _stage_schema, _result_batch_issues
    diagnostics = _result_batch_issues(batch)
    if not diagnostics:
        return batch
    schema = load_schema()
    combined = {**context, 'results': [*context.get('results', []), *batch['results']]}
    known = objects(combined)
    ids = sorted(key for key in known if not re.search(r'\.[GSTA]\d+$', key))
    eligible = ids
    record_ids = [record['id'] for record in batch['results']]
    owners = {record['id']: record['id'] for record in batch['results']}
    owners.update({field['id']: record['id'] for record in batch['results'] for field in record['fields']})
    old_edges = batch['relationships']
    removable = [i for i, edge in enumerate(old_edges) if edge['relation'] != 'table_for']
    edge_type = {'allOf': [schema['properties']['relationships']['items'], {
        'properties': {'source_id': {'enum': ids}, 'target_id': {'enum': ids},
                       'basis': {'type': 'string', 'minLength': 1}}, 'required': ['basis']}]}
    review_schema = _stage_schema(schema, {
        'add_relationships': {'type': 'array', 'maxItems': 4 * sum(len(r['fields']) for r in batch['results']) + 8 * len(record_ids), 'items': edge_type},
        'remove_relationship_indexes': {'type': 'array', 'uniqueItems': True, 'items': {'enum': removable} if removable else False},
        'core_assignments': {'type': 'object', 'properties': {key: {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'enum': eligible}} for key in record_ids}, 'additionalProperties': False},
        'review_notes': {'type': 'string', 'minLength': 1},
        'closure_review': {'type': 'array', 'items': {'allOf': [
            {'$ref': '#/$defs/ClosureReviewItem'},
            {'properties': {'object_id': {'enum': sorted(owners)}}}]}},
        'review_required_results': {'type': 'array', 'uniqueItems': True, 'items': {'enum': record_ids}},
    }, ['add_relationships', 'remove_relationship_indexes', 'core_assignments', 'review_notes'])

    def apply(review):
        trial = deepcopy(batch)
        removed = set(review['remove_relationship_indexes'])
        edges = [edge for i, edge in enumerate(old_edges) if i not in removed] + review['add_relationships']
        trial['relationships'] = list({json.dumps(edge, sort_keys=True): edge for edge in edges}.values())
        flagged = set(review.get('review_required_results', []))
        for record in trial['results']:
            if record['id'] in review['core_assignments']:
                record['closure']['core_ids'] = review['core_assignments'][record['id']]
            if record['id'] in flagged:
                record['closure'].update(status='review_required', notes=review['review_notes'])
        trial['closure_review'] = review.get('closure_review', [])
        resolve_terminal_comparison_cores(trial)
        recompute_closures(trial)
        return trial

    def check(review):
        trial = apply(review)
        flags = {record['id'] for record in trial['results'] if record['closure'].get('status')=='review_required'}
        errors = [{'message': f'{item["object_id"]}: closure review requires an existing field or record and its owning result marked review_required'}
                  for item in review.get('closure_review', []) if owners.get(item['object_id']) not in flags]
        return errors + _result_batch_issues(trial)

    contract = {key: value for key, value in schema.items() if key.startswith('x-')}
    review = _call(runtime, directory, stage, SYSTEM + '''
Review scientific graph attachments ONLY for the supplied existing objects. You may
add source-supported relationships, remove specifically indexed incorrect projections,
and review core choices. You cannot change any field value, ID, table, or source.
Use the supplied source snippets, context, documents, and vocabulary to justify links.
An explicit relationship basis must describe the scientific connection, not storage.
For descriptive counts/proportions that characterize a reported distribution or joint
test, describes (field -> identified result, source_to_target) can express that role.
For a study count that characterizes a pooled estimate, attach it to that estimate
when the source supports it; linking it only to an unrelated storage R does not work.
For an identified comparison anchor, compares is anchor -> numeric input, with
closure_direction target_to_source and evaluated/reference roles from source context.
SE/SD/CI are the SOURCE of uncertainty_of/spread_of; their matching estimate is TARGET.
Choose cores consistent with the reviewed scientific graph. A p-value qualifying an
identified comparison is not a replacement for that comparison's terminal anchor.
An independently reported baseline/reference value can use a justified none boundary
link to the result it helps interpret. Never manufacture a dependency to claim closure.
If attachment cannot be established from source, preserve the field or record in closure_review
and mark its result in review_required_results. Do not silently claim it is closed.
Retain existing valid edges. Remove an edge only when specifically correcting it,
using its supplied numeric index. Preserve source table links. Return only the graph
review schema keys; do not return or rewrite results or fields.
''', {'contract': contract, 'documents': documents,
      'records': batch['results'], 'shared_context': context.get('shared_context', []),
      'current_relationships': [{'index': i, **edge} for i, edge in enumerate(old_edges)],
      'defined_scientific_objects': [{'id': key, 'label': value.get('label'), 'field': value.get('field'), 'title': value.get('title')} for key, value in known.items()],
      'graph_diagnostics': diagnostics}, review_schema, attempts, check=check)
    return apply(review)
