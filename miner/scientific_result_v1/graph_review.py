"""Focused source-grounded relationship review without rewriting scientific values."""
from copy import deepcopy
import json
import re

from .schema import load_schema
from .validation import objects, recompute_closures, resolve_terminal_comparison_cores


def review_batch_graph(batch, *, context, documents, runtime, directory, stage, attempts):
    """Review connected results separately, then verify the complete merged graph.

    Reusable context/table links do not join unrelated results. Direct scientific
    relationships and cross-result core references keep their owners together.
    Every original field and relationship remains available; only review work is
    divided, never the submitted scientific result or its final validation.
    """
    from .pipeline import _result_batch_issues
    if not _result_batch_issues(batch):
        return batch
    records = batch['results']
    owners = {key: record['id'] for record in records
              for key in [record['id'], *(field['id'] for field in record['fields'])]}
    parent = {record['id']: record['id'] for record in records}
    def root(key):
        while parent[key] != key:
            key = parent[key]
        return key
    def join(left, right):
        if left in owners and right in owners:
            parent[root(owners[right])] = root(owners[left])
    for edge in batch['relationships']:
        if edge['relation'] not in {'table_for', 'applies_under'}:
            join(edge['source_id'], edge['target_id'])
    for record in records:
        for key in record['closure']['core_ids']:
            join(record['id'], key)
        for field in record['fields']:
            join(field['id'], field.get('parent_field_id'))
    groups = {}
    for record in records:
        groups.setdefault(root(record['id']), []).append(record)
    if len(groups) == 1:
        return _review_joint_graph(batch, context=context, documents=documents, runtime=runtime,
                                   directory=directory, stage=stage, attempts=attempts)
    result = deepcopy(batch)
    replacements = {}; edges = []; reviews = []
    for ordinal, group in enumerate(groups.values(), 1):
        owned = {record['id'] for record in group}
        selected = [edge for edge in batch['relationships'] if
            owners.get(edge['source_id']) in owned or owners.get(edge['target_id']) in owned or
            (ordinal == 1 and edge['source_id'] not in owners and edge['target_id'] not in owners)]
        part = {'results': deepcopy(group), 'relationships': selected,
                'closure_review': [item for item in batch.get('closure_review', [])
                                   if owners.get(item['object_id']) in owned]}
        # Other results are read-only context, with all original values intact.
        scope = {**context, 'results': [*context.get('results', []),
                  *(record for record in records if record['id'] not in owned)]}
        reviewed = _review_joint_graph(part, context=scope, documents=documents, runtime=runtime,
            directory=directory, stage=f'{stage}-component-{ordinal:03d}', attempts=attempts)
        replacements.update({record['id']: record for record in reviewed['results']})
        edges.extend(reviewed['relationships']); reviews.extend(reviewed.get('closure_review', []))
    result['results'] = [replacements[record['id']] for record in records]
    result['relationships'] = list({json.dumps(edge, sort_keys=True): edge for edge in edges}.values())
    result['closure_review'] = list({json.dumps(item, sort_keys=True): item for item in reviews}.values())
    resolve_terminal_comparison_cores(result)
    recompute_closures(result)
    issues = _result_batch_issues(result)
    if issues:
        raise ValueError('Merged graph review is invalid: ' + '; '.join(i['message'] for i in issues[:3]))
    return result


def _review_joint_graph(batch, *, context, documents, runtime, directory, stage, attempts):
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
        trial['closure_review'] = list({json.dumps(item, sort_keys=True): item for item in [
            *batch.get('closure_review', []), *review.get('closure_review', [])]}.values())
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
