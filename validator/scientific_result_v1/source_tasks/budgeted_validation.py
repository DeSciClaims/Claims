"""Opt-in local cost experiment; retrieval coverage is never invented."""
import json
import re
from threading import Lock

from .contracts import MatchResponse, MatchResult, SearchCoverage
from .store import canonical, sha
from .operations import Hold

PROFILE = {
    'kind': 'claims_budgeted_validation_diagnostic_v2',
    'wire_format': 'canonical_json_items_v1',
    'candidate_components': 96,
    'matching_batch_size': 4,
    'judging_batch_size': 3,
    'batch_input_target_tokens': 48000,
    'source_candidate_anchors': 12,
    'absence_policy': 'unresolved_without_exhaustive_search',
    'competition_eligible': False,
}


def profile(value):
    if value != PROFILE:
        raise ValueError('Unknown budgeted validation diagnostic profile')
    return dict(PROFILE)


class Context:
    def __init__(self):
        from .budgeted_batches import Coordinator
        self.batches = Coordinator()
        self.sources = {}
        self.lock = Lock()

    def source(self, frame):
        key = sha(canonical(frame))
        with self.lock:
            if key not in self.sources:
                self.sources[key] = SourceIndex(frame)
            return self.sources[key]


def enabled(ops):
    return getattr(ops, 'budgeted_profile', None) is not None


def match(index, task, ops, evidence, role):
    """Rank once from the reusable output index, then inspect original candidates.

    Catalog entries retain original scalar/field properties. Nested field arrays
    and closure ledgers are retrieval context, not independently inferred facts.
    The later scientific packet still contains the complete selected originals.
    """
    original_keys = {o.original_ref.pointer for o in index.objects}
    original_keys.update(r.original_ref.pointer for r in index.relations)
    exact = [key for key in index.source_hits(evidence) if key in original_keys]
    # IDs, instructions and the schema are not scientific search terms.
    query = ' '.join(r.statement + ' ' + ' '.join(r.qualifications) for r in task.requirements)
    query += ' ' + ' '.join(a.get('exact_text') or '' for a in evidence.get('anchors', []))
    ranked = [key for key in index.ranked(query) if key in original_keys]
    keys = list(dict.fromkeys(exact + ranked[:ops.budgeted_profile['candidate_components']]))
    catalog = {}
    for key in keys:
        obj = index.components[key]
        catalog[key] = {
            'original_pointer': key,
            'kind': obj.get('kind', 'RELATION'),
            'properties': [[p['name'], p['present'], p['value_json'] if p['present'] else None]
                           for p in obj['properties'] if p['name'] not in {'fields', 'closure'}],
        }
    data = dict(task=task.model_dump(mode='json'), output_alias=index.alias,
        scan_scope='SHORTLIST_ONLY_NOT_EXHAUSTIVE', context_headers_are_partial=True,
        context_headers={key: {'original_pointer': header['original_ref']['pointer'],
            'properties': [[p['name'],p['value_json']] for p in header['properties']]}
            for key,header in index.match_context(keys).items()}, inspection_component_ids=keys,
        required_requirement_ids=[r.requirement_id for r in task.requirements],
        components=catalog, authoritative_evidence=evidence)
    original_properties={p.original_ref.pointer for o in [*index.objects,*index.relations]
                         for p in o.properties if p.present}

    def validate(response):
        if response.task_id != task.task_id or response.output_alias != index.alias:
            raise ValueError('Wrong budgeted matching identity')
        if len(response.inspected_component_ids) != len(keys) or set(response.inspected_component_ids) != set(keys):
            raise ValueError('Shortlist inspection changed')
        requirements = [r.requirement_id for r in response.matches]
        if len(requirements) != len(set(requirements)) or set(requirements) != set(data['required_requirement_ids']):
            raise ValueError('Every source requirement needs a correspondence or explicit empty result')
        for item in response.matches:
            for ref in item.output_refs:
                index.resolve(ref)
                if ref.kind != 'json' or not any(ref.pointer == k or ref.pointer.startswith(k + '/') for k in keys):
                    raise ValueError('Match points outside retrieved original candidates')
                if task.scope_kind=='SOURCE_CONTEXT' and ref.pointer in index.components and ref.pointer not in original_properties:
                    raise ValueError('Context correspondence must cite original properties, not complete object/closure roots')
        assigned=[r for item in response.matches for r in item.output_refs]
        if response.state=='ABSENT' and assigned:raise ValueError('ABSENT correspondence cannot have matched references')
        if response.state in {'MATCHED','AMBIGUOUS'} and not assigned:raise ValueError('Matched correspondence needs requirement references')

    response = ops.request(role, 'source_match', data, MatchResponse, validate=validate)
    candidates = list({canonical(r): r for m in response.matches for r in m.output_refs}.values())
    # A shortlist cannot certify absence, even if the model calls it ABSENT.
    if response.state == 'LOOKUP_UNRESOLVED' or any(not m.output_refs for m in response.matches):
        raise Hold('EVALUATOR', 'Indexed retrieval did not resolve every requirement; no exhaustive omission claim')
    available = sorted(index.components, key=lambda s: s.encode())
    return MatchResult(task_id=task.task_id, output_alias=index.alias,
        state='MATCHED' if candidates else 'LOOKUP_UNRESOLVED', matches=response.matches,
        candidate_refs=candidates, coverage=SearchCoverage(index_sha256=index.sha256,
            available_component_ids=ops.store.put(available), inspected_component_ids=ops.store.put(keys),
            available_components=len(available), inspected_components=len(keys), exhaustive_fallback_completed=False),
        explanation='Experimental indexed shortlist. Original candidates are verified scientifically; omissions remain unresolved.')


def terms(text):
    return set(re.findall(r'[\w]+(?:[.,][0-9]+)?', text.casefold()))


class SourceIndex:
    """One exact-text index per frozen source frame, reused across miners/cases."""
    def __init__(self, frame):
        self.frame = frame
        self.anchors = {a.anchor_id: a for a in frame.anchors}
        self.order = {a.anchor_id: i for i, a in enumerate(frame.anchors)}
        self.tokens = {a.anchor_id: terms(a.exact_text or '') for a in frame.anchors}
        self.inverted = {}
        for aid, words in self.tokens.items():
            for word in words:
                self.inverted.setdefault(word, set()).add(aid)

    def retrieve(self, ops, task, packet):
        from .evidence_batching import mandatory, evidence, source_size, lookup_query
        from .budgets import SOURCE_TOKEN_BUDGETS
        scope = list(dict.fromkeys(task.evidence_anchor_ids))
        if not set(scope) <= self.anchors.keys():
            raise ValueError('Unknown captured source anchor')
        limit = SOURCE_TOKEN_BUDGETS[0]
        if source_size(self.frame, scope, ops.tokenizers) <= limit:
            return scope, True
        required = mandatory(self.frame, task)
        query = terms(canonical(lookup_query(packet, task)).decode())
        scores = {aid: 0.0 for aid in scope}
        for word in query:
            owners = self.inverted.get(word, set())
            weight = (4 if any(c.isdigit() for c in word) else 1) / max(len(owners), 1)
            for aid in owners & scores.keys():
                scores[aid] += weight
        # Original quotation matches are mandatory retrieval hits, never summaries.
        quoted = []
        for obj in packet.objects:
            for prop in obj.properties:
                if prop.present and prop.name == 'source_snippets':
                    value = json.loads(prop.value_json)
                    quoted.extend(s.get('text', '') for s in value if isinstance(s, dict))
        normalize = lambda s: ' '.join(s.casefold().split())
        for aid in scope:
            text = normalize(self.anchors[aid].exact_text or '')
            if any(normalize(q) and normalize(q) in text for q in quoted):
                required.add(aid)
        candidates = sorted(scope, key=lambda aid: (-scores[aid], self.order[aid]))
        chosen = set(required)
        if source_size(self.frame, chosen, ops.tokenizers) > limit:
            raise Hold('SOURCE', 'Complete mandatory source witnesses exceed the request capacity')
        for aid in candidates[:ops.budgeted_profile['source_candidate_anchors']]:
            # Keep immediate captured neighbours and all explicit tile context.
            context = {aid}
            ordinal = self.order[aid]
            for neighbour in self.frame.anchors[max(0, ordinal-1):ordinal+2]:
                if neighbour.document_uid == self.anchors[aid].document_uid and neighbour.anchor_id in scores:
                    context.add(neighbour.anchor_id)
            for tile in self.frame.tiles:
                if aid in tile.owned_anchor_ids:
                    context.update(a for a in tile.context_anchor_ids if a in scores)
            if source_size(self.frame, chosen | context, ops.tokenizers) <= limit:
                chosen.update(context)
        if not chosen:
            raise Hold('SOURCE', 'No complete captured source anchor fits indexed evidence retrieval')
        ids = [a for a in scope if a in chosen]
        # Retain the full retrieval ledger without pretending independent LLM scans ran.
        return ids, set(ids) == set(scope)


def prepare(ops, frame, shards):
    from .evidence_batching import evidence
    result = []
    source = ops.budgeted_context.source(frame)
    for task, packet in shards:
        ids, complete = source.retrieve(ops, task, packet)
        summary = dict(kind='indexed_source_lookup_diagnostic_v1', complete_for_scope=complete,
            scope_anchor_ids=ops.store.put(task.evidence_anchor_ids).model_dump(mode='json'),
            selected_anchor_ids=ids, independent_roles=[], no_support_is_not_proven_absence=not complete)
        receipt = ops.store.put(summary)
        summary['receipt'] = receipt.model_dump(mode='json')
        packet = packet.model_copy(update={'evidence_anchor_ids': ids})
        packet = packet.model_copy(update={'packet_id': 'packet-' + sha(canonical(packet.model_dump(exclude={'packet_id'})))})
        result.append((task, packet, evidence(frame, ids), summary))
    return result
