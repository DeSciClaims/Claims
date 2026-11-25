"""Coalesce independent DSPy cases without sharing candidates or judge opinions."""
from concurrent.futures import Future
from collections import defaultdict
from threading import Lock, Thread
import copy
import time
from typing import Any

from .contracts import Closed, Id, Sha256, MatchResponse, JudgeResponse, FileRef, JudgeRunReceipt
from .store import canonical, sha
from .operations import Hold


class BatchItem(Closed):
    case_id: Id
    result_json: Id


class BatchResponse(Closed):
    items: list[BatchItem]


class InspectionManifest(Closed):
    manifest_sha256: Sha256


class StructuredMatch(MatchResponse):
    inspected_component_ids: list[Id] | InspectionManifest


class MatchItem(Closed):
    case_id: Id
    result: StructuredMatch


class JudgeItem(Closed):
    case_id: Id
    result: JudgeResponse


class MatchBatchSchema(Closed):
    items: list[MatchItem]


class JudgeBatchSchema(Closed):
    items: list[JudgeItem]


class StructuredItem(Closed):
    case_id: Id
    result: dict[str, Any]


class MatchBatch(Closed):
    items: list[StructuredItem]

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return MatchBatchSchema.model_json_schema(*args, **kwargs)


class JudgeBatch(Closed):
    items: list[StructuredItem]

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return JudgeBatchSchema.model_json_schema(*args, **kwargs)


def structured_response(response_type):
    if response_type is MatchResponse:return MatchBatch
    if response_type is JudgeResponse:return JudgeBatch
    raise ValueError('Unsupported structured batch result contract')


def result_json(record):
    # Programmatic serialization of a typed object, never model-authored JSON
    # hidden inside a string. Keep legacy receipt readers available separately.
    return canonical(record.result).decode()


def portable_response_schema(schema, data):
    """Provider generation uses the canonical shape; code validates each case.

    Nested conditional unions are not portable across the approved providers.
    Removing generation-only conditionals never relaxes per-case validation.
    """
    definitions=schema['$defs']
    if data['original_operation']!='source_match':
        check=definitions['DimensionCheck']
        if 'anyOf' in check:
            plain=copy.deepcopy(check['anyOf'][0]);props=plain['properties']
            props['status']={'type':'string','enum':['PASS','FAIL','NOT_APPLICABLE']}
            props['counterexamples'].pop('maxItems',None)
            props['counterexamples'].pop('minItems',None)
            props['evidence_anchor_ids'].pop('minItems',None)
            definitions['DimensionCheck']=plain
    if data.get('budgeted_response_format')=='json_object':
        schema['x-claims-budgeted-json-object']=True
    return schema


SUPPORTED = {'source_match', 'primary_verify', 'secondary', 'arbitrate'}


def witness_aliases(payload, key):
    """Resolve only exact source entries explicitly supplied to this case."""
    cases=[c for c in payload['cases'] if c['case_id']==key]
    if len(cases)!=1:raise ValueError('Ambiguous source-alias case binding')
    source=cases[0]['payload'].get('authoritative_evidence',{})
    aliases={}
    for token in source.get('shared_source_anchor_refs',[]):
        anchor=payload['shared_source_anchors'][token]
        if sha(canonical(anchor))!=token:raise ValueError('Source alias does not bind its exact original anchor')
        aliases[token]=anchor['anchor_id']
    if any(token in aliases.values() and token!=original for token,original in aliases.items()):
        raise ValueError('Source alias collides with an original anchor identity')
    return aliases


def decode_result(raw, response_type, data, *, source_aliases=None):
    """Decode the existing keyed wire forms without repairing scientific content."""
    from .literals import LiteralDocument
    parsed=LiteralDocument(raw.encode()).value
    if response_type is JudgeResponse and source_aliases:
        def remap(values):return [source_aliases.get(value,value) for value in values]
        checks=parsed.get('checks',[])
        for check in checks.values() if isinstance(checks,dict) else checks:
            if 'evidence_anchor_ids'in check:check['evidence_anchor_ids']=remap(check['evidence_anchor_ids'])
            for counter in check.get('counterexamples',[]):
                if 'evidence_anchor_ids'in counter:counter['evidence_anchor_ids']=remap(counter['evidence_anchor_ids'])
        if parsed.get('target_issue') and 'evidence_anchor_ids'in parsed['target_issue']:
            parsed['target_issue']['evidence_anchor_ids']=remap(parsed['target_issue']['evidence_anchor_ids'])
        for request in parsed.get('evidence_requests',[]):
            if 'anchor_ids'in request:request['anchor_ids']=remap(request['anchor_ids'])
    if response_type is MatchResponse and isinstance(parsed.get('inspected_component_ids'),dict):
        declared=parsed['inspected_component_ids']
        if (data.get('scan_scope')!='COMPLETE_ORIGINAL_JSON' or set(declared)!={'manifest_sha256'}
            or declared['manifest_sha256']!=data['inspection_manifest_sha256']):
            raise ValueError('Full inspection requires the exact explicitly certified component manifest')
        parsed={**parsed,'inspected_component_ids':data['inspection_component_ids']}
    if response_type is MatchResponse and isinstance(parsed.get('matches'),dict):
        expected=data['required_requirement_ids'];matches=parsed['matches']
        if set(matches)!=set(expected) or any(not isinstance(v,list) for v in matches.values()):
            raise ValueError('Keyed matches must contain every requirement exactly once')
        parsed={**parsed,'matches':[dict(requirement_id=key,output_refs=matches[key]) for key in expected]}
    if response_type is JudgeResponse and isinstance(parsed.get('checks'),dict):
        expected=data['required_dimensions'] if parsed.get('outcome')=='ASSESSED' else []
        checks=parsed['checks']
        if set(checks)!=set(expected) or any(not isinstance(v,dict) or v.get('dimension')!=k for k,v in checks.items()):
            raise ValueError('Keyed checks must contain every applicable dimension exactly once')
        parsed={**parsed,'checks':[checks[key] for key in expected]}
    return response_type.model_validate_json(canonical(parsed),strict=True)


def case_key(ops, role, operation, data):
    return sha(canonical(dict(kind='budgeted_case_v1', profile=ops.budgeted_profile,
        route=ops.routes[role].route_sha256, policy=ops.policy_hash, role=role, operation=operation, payload=data)))


def pooled(items):
    """Exact shared values; IDs address original bytes, never generated summaries."""
    from .transport import audit_evidence
    anchors = {}; values = {}; cases = []
    def shared(value):
        key = sha(canonical(value)); values[key] = value
        return {'shared_value_ref': key}
    for item in items:
        data = copy.deepcopy(item['data'])
        source = data.get('authoritative_evidence')
        if source:
            source = audit_evidence(source) if source.get('evidence_encoding') else source
            ids = []
            for anchor in source['anchors']:
                key = sha(canonical(anchor)); anchors[key] = anchor; ids.append(key)
            data['authoritative_evidence'] = {'shared_source_anchor_refs': ids}
        if isinstance(data.get('components'), dict):
            data['components'] = {key: shared(value) for key, value in data['components'].items()}
        for name in ('original_extraction_json','computed_closure_views','inspection_component_ids'):
            if name in data:data[name]=shared(data[name])
        if isinstance(data.get('packet'), dict):
            for name in ('objects', 'relations', 'context_headers', 'closures', 'csv_blocks', 'document_bindings'):
                if name in data['packet']:
                    data['packet'][name] = shared(data['packet'][name])
        cases.append({'case_id': item['key'], 'payload': data})
    return dict(cases=cases, shared_values=values, shared_source_anchors=anchors)


class Coordinator:
    def __init__(self):
        self.lock = Lock()
        self.pending = defaultdict(list)
        self.running = set()

    def submit(self, ops, role, operation, data, response_type, validate):
        key = case_key(ops, role, operation, data)
        cached = ops.store.head('budgeted-case:' + key)
        if cached is not None:
            record = ops.store.get_blob(cached)
            import json
            saved = json.loads(record)
            if saved['case_id'] != key or saved['kind'] != 'budgeted_case_receipt_v1':
                raise ValueError('Cached budgeted case binding changed')
            if saved['error'] is not None:
                saved=self.recover_wire(ops,key,cached,saved,response_type,data,validate)
                if saved['error'] is not None:
                    self.retain(ops,saved)
                    raise Hold('MODEL', 'Retained budgeted case failure; owner disabled retries')
            result = response_type.model_validate(saved['result'], strict=True)
            successful = [ops.store.load(FileRef.model_validate(ref, strict=True), JudgeRunReceipt)
                          for ref in saved['run_refs']]
            if not any(not run.error and run.role == role and run.parsed_response.sha256 == saved['batch_response_sha256']
                       for run in successful if run.parsed_response is not None):
                raise ValueError('Cached item lacks its original paid batch response')
            if validate: validate(result)
            self.retain(ops, saved)
            return result
        alias = data.get('output_alias') or data.get('packet', {}).get('output_alias')
        if not alias:
            raise ValueError('Batching must remain scoped to one submitted output')
        group = (role, operation, alias)
        item = dict(key=key, ops=ops, role=role, operation=operation, data=data,
                    response_type=response_type, validate=validate, future=Future())
        with self.lock:
            self.pending[group].append(item)
            if group not in self.running:
                self.running.add(group)
                Thread(target=self.flush, args=(group,ops.budgeted_profile['coalescing_window_seconds']), daemon=True).start()
        try:
            result, saved = item['future'].result(timeout=max(.01, ops.deadline-time.time()))
        except TimeoutError as exc:
            raise Hold('EVALUATOR', 'Deadline expired while waiting for a batched case') from exc
        except Exception:
            cached=ops.store.head('budgeted-case:'+key)
            if cached is not None:
                import json
                self.retain(ops,json.loads(ops.store.get_blob(cached)))
            raise
        self.retain(ops, saved)
        return result

    @staticmethod
    def retain(ops, saved):
        ops.run_refs.extend(FileRef.model_validate(ref, strict=True) for ref in saved['run_refs'])
        ops.used.extend(saved['run_ids'])
        # Derived batch receipts have their own durable budgeted-case heads.
        # They are not AssertionPacket objects and must not enter that ledger.

    @staticmethod
    def recover_wire(ops,key,previous,saved,response_type,data,validate):
        """Re-decode a paid valid batch; never retry a model or invent a decision."""
        if saved['error'] not in {'ValidationError','ValueError'} or not saved['batch_response_sha256']:
            return saved
        try:
            import json
            answer=structured_response(response_type).model_validate_json(
                ops.store.get_blob(saved['batch_response_sha256']),strict=True)
            runs=[ops.store.load(FileRef.model_validate(ref,strict=True),JudgeRunReceipt) for ref in saved['run_refs']]
            if not any(not run.error and run.role==saved['role'] and run.parsed_response is not None
                       and run.parsed_response.sha256==saved['batch_response_sha256'] for run in runs):return saved
            payload=json.loads(ops.store.read(FileRef.model_validate(saved['batch_payload'],strict=True)))
            expected={case['case_id'] for case in payload['cases']}
            ids=[record.case_id for record in answer.items]
            if len(ids)!=len(set(ids)) or not set(ids)<=expected or ids.count(key)!=1:return saved
            aliases=witness_aliases(payload,key)
            result=decode_result(next(result_json(r) for r in answer.items if r.case_id==key),response_type,data,
                source_aliases=aliases)
            if validate:validate(result)
        except Exception:
            return saved
        recovered={**saved,'result':result.model_dump(mode='json'),'error':None,
            'wire_recovery':dict(previous_receipt_sha256=previous,
                decoder='canonical_lists_keyed_maps_and_case_scoped_source_aliases_v2',
                source_aliases=ops.store.put(aliases).model_dump(mode='json'),provider_calls=0)}
        ref=ops.store.put(recovered);ops.store.advance('budgeted-case:'+key,previous,ref.sha256)
        return recovered

    def flush(self, group, window):
        while True:
            time.sleep(window)  # Coalesce ready work across CPU preparation; never await case dependencies.
            with self.lock:
                if not self.pending[group]:
                    self.running.remove(group); del self.pending[group]; return
                first = self.pending[group][0]
                limit = first['ops'].budgeted_profile['matching_batch_size' if group[1] == 'source_match' else 'judging_batch_size']
                items = self.pending[group][:limit]; del self.pending[group][:len(items)]
            # Stable ordering and item-level caches allow interruption-safe reuse.
            items.sort(key=lambda item: item['key'])
            # Independent batches do not wait on earlier calls for this miner.
            # The existing global request gate still owns provider admission.
            Thread(target=self.dispatch_safe,args=(items,),daemon=True).start()

    def dispatch_safe(self,items):
        try:self.dispatch(items)
        except Exception as exc:
            for item in items:
                if not item['future'].done():item['future'].set_exception(exc)

    def dispatch(self, items):
        from .operations import Operations, ROOT
        first = items[0]; ops = first['ops'].scoped(); role = first['role']; operation = first['operation']
        response_type = structured_response(first['response_type'])
        data = pooled(items)
        instructions = (ROOT / (operation + '.txt')).read_text()
        if operation == 'secondary': instructions += '\n' + (ROOT / 'primary_verify.txt').read_text()
        data.update(original_operation=operation, case_instructions=instructions,
            budgeted_response_format=ops.budgeted_profile['provider_response_formats'][role])
        request = ops.prepare_request(role, 'budgeted_structured_batch', data, response_type)
        size = ops.request_tokens(request)
        target = ops.routes[role].context_window_tokens-ops.routes[role].max_output_tokens
        if len(items) > 1 and (size > target or size + ops.routes[role].max_output_tokens > ops.routes[role].context_window_tokens):
            middle = len(items)//2
            self.dispatch(items[:middle]); self.dispatch(items[middle:]); return
        results = {}; batch_ref = None; error = None
        try:
            # Use the ordinary DSPy runtime, provider usage journal and durable request cache.
            answer = Operations.request(ops, role, 'budgeted_structured_batch', data, response_type)
            batch_ref = ops.store.put(answer)
            allowed = {item['key'] for item in items}
            for record in answer.items:
                if record.case_id not in allowed or record.case_id in results:
                    raise ValueError('Unknown or duplicate batched case identity')
                results[record.case_id] = result_json(record)
        except Exception as exc:
            error = exc
        for item in items:
            raw = results.get(item['key']); result = None; failure = error
            if failure is None:
                try:
                    if raw is None: raise ValueError('Batch omitted this case; other cases remain usable')
                    result=decode_result(raw,item['response_type'],item['data'],
                        source_aliases=witness_aliases(data,item['key']))
                    if item['validate']: item['validate'](result)
                except Exception as exc: failure = exc
            saved = dict(kind='budgeted_case_receipt_v1', case_id=item['key'],
                operation=operation,role=role,wire_format=ops.budgeted_profile['wire_format'],
                output_alias=item['data'].get('output_alias') or item['data'].get('packet',{}).get('output_alias'),
                batch_response_sha256=batch_ref.sha256 if batch_ref else None,
                batch_payload=ops.store.put(data).model_dump(mode='json'),
                source_aliases=ops.store.put(witness_aliases(data,item['key'])).model_dump(mode='json'),
                result=result.model_dump(mode='json') if result else None,
                error=type(failure).__name__ if failure else None,
                run_refs=[ref.model_dump(mode='json') for ref in ops.run_refs], run_ids=ops.used)
            ref = ops.store.put(saved)
            old = ops.store.head('budgeted-case:' + item['key'])
            ops.store.advance('budgeted-case:' + item['key'], old, ref.sha256)
            if failure:
                item['future'].set_exception(failure)
            else:
                item['future'].set_result((result, saved))
