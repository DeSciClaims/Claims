"""Source-only, independently witnessed inventory corrections for the common pool."""
import json
from .contracts import (FileRef,EvaluationBinding,SourceFrame,SourceTaskBatch,
    SourceReviewResponse,JudgeRunReceipt,CorrectionRecord)
from .operations import Hold
from .store import canonical,sha


class SourceInventoryDefect(Hold):
    def __init__(self,proposal):
        super().__init__('SOURCE','Selected tile has an independently confirmed inventory defect')
        self.proposal=proposal


def retain_defect(ops,tile,binding,batch,run_start):
    if batch.outcome!='INVENTORY_DEFECT':raise ValueError('Only a confirmed inventory defect can request frame correction')
    proof={'kind':'claims_source_inventory_defect_v1','binding':binding.model_dump(mode='json'),
        'tile':tile.model_dump(mode='json'),'reviewed_batch':ops.store.put(batch).model_dump(mode='json'),
        'judge_runs':[r.model_dump(mode='json') for r in ops.run_refs[run_start:]]}
    return ops.store.put(proof)


def validate_defect(store,proof_ref,frame):
    """Reconstruct the draft/review/arbiter chain; an unreviewed claim is insufficient."""
    from .transport import audit_evidence
    proof=json.loads(store.read(proof_ref))
    if proof['kind']!='claims_source_inventory_defect_v1':raise ValueError('Unknown source defect proof')
    binding=EvaluationBinding.model_validate(proof['binding'])
    if binding.source_frame_sha256!=sha(canonical(frame)) or binding.source_packet_sha256!=frame.source_packet_sha256:
        raise ValueError('Source defect has a different original frame')
    tile=next((t for t in frame.tiles if t.tile_id==proof['tile']['tile_id']),None)
    if tile is None or tile.model_dump(mode='json')!=proof['tile'] or not tile.eligible_content:
        raise ValueError('Source defect changes tile identity or eligibility')
    batch=store.load(FileRef.model_validate(proof['reviewed_batch']),SourceTaskBatch)
    if batch.tile_id!=tile.tile_id or batch.outcome!='INVENTORY_DEFECT':raise ValueError('Source defect lacks a complete defective-tile decision')
    anchors={a.anchor_id:a.model_dump(mode='json') for a in frame.anchors}
    drafts=[];reviews=[]
    for ref in proof['judge_runs']:
        run=store.load(FileRef.model_validate(ref),JudgeRunReceipt)
        request=json.loads(store.read(run.request))
        original={k:v for k,v in request.items() if k!='validation_feedback'}
        if sha(canonical(original))!=run.run_id.rsplit(':',1)[0] or request['policy_sha256']!=binding.policy_sha256:
            raise ValueError('Source defect request/policy identity changed')
        if request['role']!=run.role or request['route']['route_sha256']!=run.model_route_sha256:
            raise ValueError('Source defect reviewer identity changed')
        if run.error or run.parsed_response is None:continue
        data=request['payload']
        if any(k in data for k in ('packet','candidate','candidate_output','submitted_package')):
            raise ValueError('Source defect review used a candidate representation')
        if data.get('tile')!=tile.model_dump(mode='json'):raise ValueError('Source defect review changed owned source tile')
        evidence=data['authoritative_evidence']
        if 'evidence_encoding' in evidence:evidence=audit_evidence(evidence)
        ids=[a['anchor_id'] for a in evidence['anchors']]
        if not set(tile.owned_anchor_ids)<=set(ids) or any(a!=anchors.get(a['anchor_id']) for a in evidence['anchors']):
            raise ValueError('Source defect review changed captured evidence')
        operation=request['operation']
        identity=(request['route']['provider'],request['route']['model'],request['route']['runtime_revision'])
        if run.role=='SOURCE_READER' and operation in {'source_card','source_card_consolidate','source_card_atomize'}:
            drafts.append((store.load(run.parsed_response,SourceTaskBatch),identity))
        elif (run.role,operation) in {('SECONDARY','source_card_review'),('ARBITER','source_card_reconcile')}:
            review=store.load(run.parsed_response,SourceReviewResponse)
            proposal=SourceTaskBatch.model_validate(data['proposal'])
            if review.request_id!=data['request_id'] or review.proposal_kind!='CARD_BATCH' or review.proposal_hash!=sha(canonical(proposal)):
                raise ValueError('Source defect independent review binding changed')
            if review.outcome not in {'ACCEPT','REVISE'}:continue
            if set(review.checked_anchor_ids)!=set(ids) or len(review.checked_anchor_ids)!=len(ids):
                raise ValueError('Source defect lacks complete source inspection')
            if review.checked_item_ids!=data['checked_item_ids']:raise ValueError('Source defect lacks complete proposal inspection')
            expected=(['original:'+c['local_key'] for c in data.get('original_source_draft',{}).get('cards',[])]+
                ['proposed:'+c.local_key for c in proposal.cards]) or ['tile:'+tile.tile_id]
            if set(expected)!=set(review.checked_item_ids):raise ValueError('Source defect inspected the wrong source items')
            reviews.append((run.role,review,proposal,data,identity))
    for role,review,proposal,data,identity in reviews:
        if role!='SECONDARY':continue
        draft=next((i for p,i in drafts if p==proposal),None)
        if draft is None or draft==identity:continue
        if review.outcome=='ACCEPT' and proposal==batch:return tile,batch,binding
        if review.outcome=='REVISE':
            for arb_role,arb,arb_proposal,arb_data,arb_identity in reviews:
                if arb_role!='ARBITER' or arb_identity in {draft,identity} or arb_proposal!=proposal:
                    continue
                if arb_data.get('independent_review')!=review.model_dump(mode='json'):continue
                decisive=proposal if arb.outcome=='ACCEPT' else arb.corrected_proposal.payload
                if decisive==batch:return tile,batch,binding
    raise ValueError('Source defect lacks a distinct independent confirmed review')


def corrected_frame(store,packet,frame,proposal):
    from .docling_frame import derive_frame,validate_docling_frame
    validate_docling_frame(store,frame,packet)
    tile,batch,_=validate_defect(store,proposal,frame)
    config=json.loads(store.get_blob(frame.parser_config_sha256))
    corrections=config.get('eligibility_corrections',[])
    if len(corrections)>=2:raise Hold('EVALUATOR','Two common source/frame corrections exhausted')
    item={'tile_id':tile.tile_id,'eligible':False,'reason':batch.unresolved_reason}
    config['eligibility']=[v for v in config['eligibility'] if v['tile_id']!=tile.tile_id]+[item]
    config['eligibility_corrections']=[*corrections,{'previous_frame':store.put(frame).model_dump(mode='json'),
        'defect_proof':proposal.model_dump(mode='json')}]
    result=derive_frame(store,packet,config,store.put(config).sha256)
    validate_docling_frame(store,result,packet)
    return result


def corrected_eligibility(store,config,expected,packet):
    """Verify each immutable frame predecessor before applying its one eligibility change."""
    result={v['tile_id']:v for v in expected}
    corrections=config.get('eligibility_corrections',[])
    if len(corrections)>2:raise ValueError('Common source correction limit exceeded')
    changed=set()
    for ordinal,correction in enumerate(corrections):
        previous=store.load(FileRef.model_validate(correction['previous_frame']),SourceFrame)
        from .docling_frame import validate_docling_frame
        if not validate_docling_frame(store,previous,packet):raise ValueError('Source correction lacks a verified original Docling frame')
        prior=json.loads(store.get_blob(previous.parser_config_sha256))
        for key in set(prior)|set(config):
            if key not in {'eligibility','eligibility_corrections'} and prior.get(key)!=config.get(key):
                raise ValueError('Inventory correction changed source derivation inputs')
        if prior.get('eligibility_corrections',[])!=corrections[:ordinal]:raise ValueError('Source correction predecessor chain changed')
        tile,batch,_=validate_defect(store,FileRef.model_validate(correction['defect_proof']),previous)
        if tile.tile_id in changed:raise ValueError('Source tile corrected twice')
        changed.add(tile.tile_id)
        result[tile.tile_id]={'tile_id':tile.tile_id,'eligible':False,'reason':batch.unresolved_reason}
    return list(result.values())


def validate_frame_scope(store,frame,binding):
    config=json.loads(store.get_blob(frame.parser_config_sha256))
    for correction in config.get('eligibility_corrections',[]):
        proof=json.loads(store.read(FileRef.model_validate(correction['defect_proof'])))
        for key in ('protocol_id','round_id','stage','paper_id','source_packet_sha256','input_pool_sha256','policy_sha256'):
            if proof['binding'][key]!=getattr(binding,key):raise ValueError('Source correction changed the common evaluation scope')


def correction_record(store,previous,previous_ref,proposal):
    proof=json.loads(store.read(proposal));tile=proof['tile']
    return CorrectionRecord(correction_id='inventory-'+proposal.sha256,kind='SOURCE_FRAME',
        superseded_receipt_sha256=previous_ref.sha256,affected_task_ids=['tile:'+tile['tile_id']],
        evidence_anchor_ids=tile['owned_anchor_ids'],
        explanation='Independent source-only inventory correction; same original outputs and paper seed; proof '+proposal.sha256)
