"""Source-only review before a parser row-count disagreement can fail a miner."""
import json
from typing import Literal
from pydantic import model_validator
from .contracts import Closed,Id,Sha256,Nonnegative,JudgeRunReceipt
from .operations import Hold
from .store import canonical,sha
from .transport import audit_evidence
from .budgets import SOURCE_TOKEN_BUDGETS

FAILURE_PREFIX='Independently verified source data-row count differs: '


class TableCountReview(Closed):
    request_id:Id
    table_uid:Id
    source_frame_sha256:Sha256
    outcome:Literal['VERIFIED','SOURCE_UNRESOLVED']
    verified_data_row_count:Nonnegative|None
    inspected_anchor_ids:list[Id]
    evidence_anchor_ids:list[Id]
    explanation:Id
    @model_validator(mode='after')
    def consistent(self):
        if (self.outcome=='VERIFIED')!=(self.verified_data_row_count is not None):raise ValueError('Only verified source layout has a count')
        return self


def validate_response(response,data):
    if (response.request_id,response.table_uid,response.source_frame_sha256)!=(data['request_id'],data['table_uid'],data['source_frame_sha256']):raise ValueError('Source count review identity changed')
    evidence=data['authoritative_evidence']
    if 'evidence_encoding' in evidence:evidence=audit_evidence(evidence)
    ids=[a['anchor_id'] for a in evidence['anchors']]
    if response.inspected_anchor_ids!=ids:raise ValueError('Source count review must inspect every supplied anchor exactly')
    if len(response.evidence_anchor_ids)!=len(set(response.evidence_anchor_ids)) or not set(response.evidence_anchor_ids)<=set(ids):raise ValueError('Source count witness is not captured evidence')
    if response.outcome=='VERIFIED' and (response.verified_data_row_count!=data['expected_data_row_count'] or not set(data['owned_data_row_anchor_ids'])<=set(response.evidence_anchor_ids)):
        raise ValueError('Verified source count must witness every original owned data row')


def batches(ops,frame,table):
    by_id={a.anchor_id:a for a in frame.anchors};context=list(dict.fromkeys(table.header_anchor_ids+table.note_anchor_ids))
    frame_hash=sha(canonical(frame));pending=[];result=[]
    def payload(owned):
        ids=list(dict.fromkeys(context+owned))
        base={'table_uid':table.table_uid,'source_frame_sha256':frame_hash,'native_layout_sha256':table.layout_receipt.sha256,
              'owned_data_row_anchor_ids':owned,'expected_data_row_count':len(owned),
              'authoritative_evidence':{'anchors':[by_id[k].model_dump(mode='json') for k in ids]}}
        return {'request_id':'source-count-'+sha(canonical(base)),**base}
    def fits(owned):
        value=canonical(payload(owned)['authoritative_evidence']).decode()
        return max(t(value) for t in ops.tokenizers.values())<=SOURCE_TOKEN_BUDGETS[0]-1024
    for aid in table.data_row_anchor_ids:
        if pending and not fits(pending+[aid]):result.append(payload(pending));pending=[]
        if not fits([aid]):raise Hold('SOURCE','One complete source row and its headers exceed the source-review bound')
        pending.append(aid)
    if pending:result.append(payload(pending))
    return result


def verify_source_count(ops,frame,table):
    """Never transmit candidate rows/counts or use a candidate to repair source truth."""
    for data in batches(ops,frame,table):
        validate=lambda response:validate_response(response,data)
        primary=ops.request('PRIMARY','source_table_count',data,TableCountReview,validate=validate)
        secondary=ops.request('SECONDARY','source_table_count',data,TableCountReview,validate=validate)
        if primary.outcome==secondary.outcome=='VERIFIED':continue
        final=ops.request('ARBITER','source_table_count',{**data,'independent_source_reviews':[primary.model_dump(mode='json'),secondary.model_dump(mode='json')]},TableCountReview,validate=validate)
        if final.outcome!='VERIFIED':raise Hold('SOURCE','Independent review could not establish source table row boundaries: '+table.table_uid)


def replay_source_count(store,receipt,frame,table):
    """Require complete source-only witnesses before replaying the new G=0 finding."""
    groups={};known={a.anchor_id:a.model_dump(mode='json') for a in frame.anchors}
    for ref in receipt.judge_runs:
        run=store.load(ref,JudgeRunReceipt)
        request=json.loads(store.read(run.request))
        if request.get('operation')!='source_table_count' or run.error is not None or run.parsed_response is None:continue
        data=request['payload']
        if data.get('table_uid')!=table.table_uid or data.get('source_frame_sha256')!=sha(canonical(frame)):continue
        if request['policy_sha256']!=receipt.binding.policy_sha256 or request['role']!=run.role or request['route']['route_sha256']!=run.model_route_sha256:raise ValueError('Source count reviewer binding changed')
        if sha(canonical({k:v for k,v in request.items() if k!='validation_feedback'}))!=run.run_id.rsplit(':',1)[0]:raise ValueError('Source count request identity changed')
        allowed={'request_id','table_uid','source_frame_sha256','native_layout_sha256','owned_data_row_anchor_ids','expected_data_row_count','authoritative_evidence','independent_source_reviews'}
        if set(data)-allowed:raise ValueError('Source count review contains candidate information')
        evidence=data['authoritative_evidence']
        if 'evidence_encoding' in evidence:evidence=audit_evidence(evidence)
        if any(a!=known.get(a['anchor_id']) for a in evidence['anchors']):raise ValueError('Source count evidence changed')
        base={k:v for k,v in data.items() if k not in {'request_id','independent_source_reviews'}}
        base['authoritative_evidence']=evidence
        if data['request_id']!='source-count-'+sha(canonical(base)):raise ValueError('Source count batch identity changed')
        owned=data['owned_data_row_anchor_ids'];available=[a['anchor_id'] for a in evidence['anchors']]
        if not owned or len(owned)!=data['expected_data_row_count'] or len(owned)!=len(set(owned)) or not set(owned)<=set(table.data_row_anchor_ids):raise ValueError('Source count ownership changed')
        positions=[table.data_row_anchor_ids.index(k) for k in owned]
        if positions!=list(range(positions[0],positions[0]+len(positions))):raise ValueError('Source count rows changed original order or continuity')
        if data['native_layout_sha256']!=table.layout_receipt.sha256 or set(available)!=set(table.header_anchor_ids+table.note_anchor_ids+owned):raise ValueError('Source count scope changed')
        response=store.load(run.parsed_response,TableCountReview);validate_response(response,data)
        identity=tuple(request['route'][k] for k in ('provider','model','runtime_revision'))
        group=groups.setdefault(data['request_id'],{'owned':owned,'reviews':{}})
        if group['owned']!=owned:raise ValueError('Source count batch changed')
        group['reviews'][run.role]=(response,identity,data)
    covered=[]
    for group in groups.values():
        reviews=group['reviews'];p=reviews.get('PRIMARY');s=reviews.get('SECONDARY');a=reviews.get('ARBITER')
        if not p or not s or p[1]==s[1]:raise ValueError('Source count lacks blind independent reviewers')
        if 'independent_source_reviews' in p[2] or 'independent_source_reviews' in s[2]:raise ValueError('Source count reviewer saw an earlier verdict')
        accepted=p[0].outcome==s[0].outcome=='VERIFIED'
        if not accepted:
            if not a or a[1] in {p[1],s[1]} or a[2].get('independent_source_reviews')!=[p[0].model_dump(mode='json'),s[0].model_dump(mode='json')] or a[0].outcome!='VERIFIED':raise ValueError('Source count lacks independent resolution')
        covered.extend(group['owned'])
    if len(covered)!=len(set(covered)) or set(covered)!=set(table.data_row_anchor_ids):raise ValueError('Source count review omitted or duplicated source rows')
