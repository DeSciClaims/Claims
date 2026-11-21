"""Source table identity is matched only against original table declarations."""
from .contracts import MatchResponse
from .operations import Hold
from .store import canonical


def resolve_table(index, ops, task, evidence):
    components = {}
    allowed = {}
    for obj in index.objects:
        if obj.kind != 'TABLE':
            continue
        pointer = obj.original_ref.pointer
        table = index.originals[obj.object_id][1]
        rows = index.package.csv[obj.object_id]
        label_columns = [i for i,c in enumerate(table['columns']) if c['role'] != 'data']
        components[pointer] = {'original_ref': obj.original_ref.model_dump(mode='json'),
            'declared_table_json': index.doc.literal(pointer),
            'csv_header': rows[0],
            'csv_row_labels': [[row[i] for i in label_columns] for row in rows[1:]]}
        allowed[canonical(obj.original_ref)] = obj.object_id
    data = {'task': task.model_dump(mode='json'), 'output_alias': index.alias,
        'scan_scope': 'TABLE_IDENTITY_ONLY', 'components': components,
        'inspection_component_ids': list(components),
        'required_requirement_ids': ['table-identity'],
        'trusted_document_bindings': index.document_uid_by_source,
        'authoritative_evidence': evidence}

    def validate(response):
        if (response.task_id, response.output_alias) != (task.task_id, index.alias):
            raise ValueError('Wrong table correspondence identity')
        if len(response.inspected_component_ids) != len(components) or set(response.inspected_component_ids) != set(components):
            raise ValueError('Table identity must inspect every original TableObject exactly once')
        if len(response.matches) != 1 or response.matches[0].requirement_id != 'table-identity':
            raise ValueError('Table identity requires one explicit correspondence group')
        selected = {canonical(r) for item in response.matches for r in item.output_refs}
        if not selected <= allowed.keys():
            raise ValueError('Table identity can select only original TableObject roots, not field/context references')
        if response.state == 'ABSENT' and selected:
            raise ValueError('Absent table correspondence cannot select a table')
        if response.state in {'MATCHED','AMBIGUOUS'} and not selected:
            raise ValueError('Matched table correspondence needs an original TableObject')

    def call(role, extra=None):
        return ops.request(role, 'source_match', {**data, **(extra or {})}, MatchResponse, validate=validate)
    primary = call('PRIMARY')
    secondary = call('SECONDARY')  # Blind to the primary correspondence.
    refs = lambda r: {canonical(x) for item in r.matches for x in item.output_refs}
    if primary.state == secondary.state and primary.state in {'MATCHED','ABSENT'} and refs(primary) == refs(secondary) and len(refs(primary)) <= 1:
        final = secondary
    else:
        final = call('ARBITER', {'adjudication_requested': True,
            'independent_table_reviews': [primary.model_dump(mode='json'),secondary.model_dump(mode='json')]})
    if final.state not in {'MATCHED','ABSENT'}:
        raise Hold('EVALUATOR', 'Source-table correspondence remains unresolved after independent arbitration')
    return {allowed[canonical(r)] for item in final.matches for r in item.output_refs}
