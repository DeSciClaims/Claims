"""Check actual DSPy HTTP requests and complete, response-bounded lookup batches."""
import copy
import json
from collections import Counter
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from tests.source_tasks.test_docling_frame import fixture
from validator.scientific_result_v1.source_tasks.contracts import EvidenceBatchResponse
from validator.scientific_result_v1.source_tasks.response_schema import response_schema,compact_schema
from validator.scientific_result_v1.source_tasks.runtime import SourceTaskRuntime,provider_request,provider_input_tokens
from validator.scientific_result_v1.source_tasks.store import canonical
from validator.scientific_result_v1.source_tasks.evidence_batching import fit_lookup_request
from validator.scientific_result_v1.source_tasks.tokenization import BudgetTokenizer


def lookup(anchor_count=800,query_count=30):
    anchors=[{'anchor_id':f'd1-text{i}-s0','document_uid':'d1','source_sha256':'a'*64,
              'exact_text':f'Original reported estimate {i}.3500 ± 0.20; n = 53.'} for i in range(anchor_count)]
    ids=[a['anchor_id'] for a in anchors];queries=[f'query-{i}' for i in range(query_count)]
    return {'request_id':'lookup-unchanged','inspected_anchor_ids':ids,'inspected_query_ids':queries,
            'queries':[{'query_id':q,'assertions':{'original_value_json':'3.500','qualifier':'adjusted'}} for q in queries],
            'authoritative_evidence':{'anchors':anchors}}


def test_large_anchor_enums_are_shared_without_weakening_schema_or_mutating_input():
    data=lookup();schema=response_schema(EvidenceBatchResponse,data);original=copy.deepcopy(schema)
    # The source enum is declared once, independently of the number of queries.
    encoded=canonical(schema).decode()
    assert encoded.count('"enum"')==1
    assert len(encoded)<40000
    answer={'request_id':data['request_id'],'inspected_anchor_ids':data['inspected_anchor_ids'],
            'inspected_query_ids':data['inspected_query_ids'],
            'witnesses':{q:[data['inspected_anchor_ids'][0]] for q in data['inspected_query_ids']}}
    for candidate in (schema,compact_schema(schema)):
        jsonschema.validate(answer,candidate)
        invalid=copy.deepcopy(answer);invalid['witnesses']['query-0']=['not-an-original-anchor']
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,candidate)
        invalid=copy.deepcopy(answer);invalid['witnesses'].pop('query-1')
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,candidate)
    assert schema==original
    duplicated={'type':'object','properties':{k:{'type':'string','enum':data['inspected_anchor_ids']} for k in data['inspected_query_ids']}}
    compact=compact_schema(duplicated)
    assert len(canonical(compact))*10<len(canonical(duplicated))
    assert compact_schema(compact)==compact


def test_dspy_http_body_has_one_schema_and_matches_preflight(monkeypatch,tmp_path):
    data=lookup(3,2);schema=compact_schema(response_schema(EvidenceBatchResponse,data))
    payload={**data,'stage_schema':schema};untouched=copy.deepcopy(payload);sent=[]
    answer={'request_id':data['request_id'],'inspected_anchor_ids':data['inspected_anchor_ids'],
            'inspected_query_ids':data['inspected_query_ids'],'witnesses':{q:[] for q in data['inspected_query_ids']}}
    async def send(client,request,*args,**kwargs):
        body=json.loads(request.content);sent.append(body)
        return httpx.Response(200,request=request,json={'id':'offline-strict','object':'chat.completion','created':1,
            'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{
                'role':'assistant','content':json.dumps(answer)}}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
    monkeypatch.setattr(httpx.AsyncClient,'send',send)
    runtime=SourceTaskRuntime(model='deepseek/deepseek-v4-flash-0731',api_key='test',reasoning_effort='none')
    actual=runtime.generate(instructions='Inspect every original anchor.',payload=payload,
                            output_dir=tmp_path/'requests',stage='source-task-source_evidence_lookup')
    assert actual==answer and payload==untouched and len(sent)==1
    route=SimpleNamespace(model=runtime.model,max_output_tokens=runtime.max_tokens)
    expected=provider_request('Inspect every original anchor.',payload,route,reasoning_effort='none')
    assert {k:sent[0][k] for k in expected}==expected
    assert 'stage_schema' not in json.loads(sent[0]['messages'][1]['content'])
    assert json.loads(sent[0]['messages'][1]['content'])==data
    assert sent[0]['response_format']['json_schema']['schema']==schema
    counter=BudgetTokenizer()
    assert provider_input_tokens(expected,counter)==provider_input_tokens({k:sent[0][k] for k in expected},counter)


def test_lookup_reply_batches_cover_every_anchor_query_pair_within_existing_output_limit(tmp_path,monkeypatch):
    _,_,ops=fixture(tmp_path,monkeypatch,context_window_tokens=262144)
    counter=BudgetTokenizer();ops.tokenizers={role:counter for role in ops.routes};data=lookup(800,30)
    before=copy.deepcopy(data)
    for role in ('PRIMARY','SECONDARY'):
        batches=fit_lookup_request(ops,role,data)
        assert len(batches)>1
        coverage=Counter()
        for batch in batches:
            request=ops.prepare_request(role,'source_evidence_lookup',batch,EvidenceBatchResponse)
            assert ops.request_tokens(request)+ops.routes[role].max_output_tokens<=ops.routes[role].context_window_tokens
            maximal={'request_id':batch['request_id'],'inspected_anchor_ids':batch['inspected_anchor_ids'],
                     'inspected_query_ids':batch['inspected_query_ids'],
                     'witnesses':{q:batch['inspected_anchor_ids'] for q in batch['inspected_query_ids']}}
            assert counter(canonical(maximal).decode())<=16384*7//8
            for q in batch['inspected_query_ids']:
                for aid in batch['inspected_anchor_ids']:coverage[(q,aid)]+=1
            for anchor in batch['authoritative_evidence']['anchors']:
                assert anchor==next(a for a in data['authoritative_evidence']['anchors'] if a['anchor_id']==anchor['anchor_id'])
        assert coverage==Counter({(q,a):1 for q in data['inspected_query_ids'] for a in data['inspected_anchor_ids']})
    assert data==before


def test_context_fitting_splits_source_without_dropping_text(tmp_path,monkeypatch):
    _,_,ops=fixture(tmp_path,monkeypatch,context_window_tokens=35000)
    counter=BudgetTokenizer();ops.tokenizers={r:counter for r in ops.routes}
    data=lookup(4,1)
    for anchor in data['authoritative_evidence']['anchors']:anchor['exact_text']='Original numerical result 3.5000. '*700
    parent=ops.prepare_request('SECONDARY','source_evidence_lookup',data,EvidenceBatchResponse)
    assert ops.request_tokens(parent)+16384>35000
    batches=fit_lookup_request(ops,'SECONDARY',data)
    assert len(batches)>1
    assert [a for b in batches for a in b['inspected_anchor_ids']]==data['inspected_anchor_ids']
    for batch in batches:
        request=ops.prepare_request('SECONDARY','source_evidence_lookup',batch,EvidenceBatchResponse)
        assert ops.request_tokens(request)+16384<=35000
