"""Prepare shared source/frame and commit hidden paper seeds before worker execution."""
from collections import deque
import secrets
from .source import SourcePreparation,text_anchors,assemble_frame,validate_frame
from .parsing import pdf_anchors,split_page_anchor
from .sampling import seed_commitment,PROTOCOL
from .store import sha,canonical,Conflict
from .operations import Hold


def prepare_paper(*,store,packet,parsed_directories,operations,stages,policy_sha256,sampling_secrets=None):
    from .contracts import SourceFrame,FileRef
    import json
    old=store.head('prepared-source:'+packet.round_id+':'+packet.paper_id)
    if old is not None:
        saved=json.loads(store.get_blob(old))
        if saved['source_packet']['sha256']!=sha(canonical(packet)):raise ValueError('Prepared source packet changed; fresh common executions required')
        for stage in stages:
            seed=saved['seeds_by_stage'][stage]
            secret=store.read(FileRef.model_validate(seed['secret'],strict=True))
            if sampling_secrets is not None and sampling_secrets[stage]!=secret:raise ValueError('Requested comparison sampling seed changed')
            if seed['commitment']!=seed_commitment(secret,(PROTOCOL,packet.round_id,stage,packet.paper_id),sha(canonical(packet)),policy_sha256):raise ValueError('Prepared seed/policy binding changed')
        frame=store.load(FileRef.model_validate(saved['source_frame'],strict=True),SourceFrame);validate_frame(frame,packet)
        raw=store.get_blob(old)
        return saved,FileRef(relative_path='blobs/'+old,sha256=old,byte_length=len(raw),media_type='application/octet-stream')
    if packet.state!='READY':raise Hold('SOURCE','Controller source packet is not ready')
    if len([d for d in packet.documents if d.role=='MAIN'])!=1:raise ValueError('Exactly one main source required')
    total=sum(d.file.byte_length for d in packet.documents if d.file is not None)
    if total>210_000_000:raise Hold('SOURCE','Common input bundle exceeds worker profile')
    pdf_mode=any(d.file is not None and d.file.media_type=='application/pdf' for d in packet.documents)
    if pdf_mode:
        from .docling_frame import prepare_docling_frame
        frame=prepare_docling_frame(store,packet,parsed_directories,operations,policy_sha256)
        validate_frame(frame,packet)
        return commit_prepared_source(store,packet,frame,operations,stages,policy_sha256,sampling_secrets=sampling_secrets)
    anchors=[];pages=[];parsers=[]
    for document in packet.documents:
        if document.file is None:
            if document.required:raise Hold('SOURCE','A required supplied source is unavailable')
            continue
        raw=store.read(document.file)
        if document.file.media_type=='application/pdf':
            doc_anchors,doc_pages,parser=pdf_anchors(store,document,raw,parsed_directories[document.document_uid])
            anchors.extend(doc_anchors);pages.extend(doc_pages);parsers.append(parser)
        else:
            doc_anchors=text_anchors(document,raw);anchors.extend(doc_anchors)
            # Source-only ordered chunks; no candidate organization enters preparation.
            for start in range(0,len(doc_anchors),12):pages.append({'owned_anchor_ids':[a.anchor_id for a in doc_anchors[start:start+12]],'context_anchor_ids':[]})
            parsers.append(sha(canonical({'source':document.file.sha256,'parser':'frozen-text-sentences-v1'})))
    preparation=SourcePreparation(operations,packet,anchors,sha(canonical(parsers)))
    pending=deque((p,0) for p in pages);inventories=[]
    while pending:
        page,depth=pending.popleft()
        try:inventory=preparation.inventory_page(page['owned_anchor_ids'],context=page['context_anchor_ids'])
        except Hold as exc:
            if 'Source scope exceeds' not in str(exc) or depth>=8:raise
            ids=page['owned_anchor_ids']
            if len(ids)>1:
                midpoint=len(ids)//2
                for group in reversed([ids[:midpoint],ids[midpoint:]]):pending.appendleft(({'owned_anchor_ids':group,'context_anchor_ids':page['context_anchor_ids']},depth+1))
            else:
                children=split_page_anchor(store,preparation.anchors[ids[0]])
                for child in reversed(children):
                    preparation.anchors[child.anchor_id]=child
                    context=[]
                    for key in page['context_anchor_ids']:
                        anchor=preparation.anchors[key]
                        if anchor.locator.kind!='page':continue
                        a,b,c,d=anchor.locator.bbox;x0,y0,x1,y1=child.locator.bbox
                        if max(a,x0)<min(c,x1) and max(b,y0)<min(d,y1):
                            # OCR is context; use the actual captured text without repeating a full page image.
                            copy=anchor.model_copy(update={'rendering':None})
                            if copy.exact_text is not None:
                                preparation.anchors[key]=copy;context.append(key)
                    pending.appendleft(({'owned_anchor_ids':[child.anchor_id],'context_anchor_ids':context},depth+1))
            continue
        inventories.append(inventory)
    frame=assemble_frame(preparation,inventories);validate_frame(frame,packet)
    return commit_prepared_source(store,packet,frame,operations,stages,policy_sha256,sampling_secrets=sampling_secrets)


def commit_prepared_source(store,packet,frame,operations,stages,policy_sha256,sampling_secrets=None):
    """Commit one immutable source frame and its pre-execution sampling secrets."""
    seeds={}
    for stage in stages:
        scope=(PROTOCOL,packet.round_id,stage,packet.paper_id)
        secret=sampling_secrets[stage] if sampling_secrets is not None else secrets.token_bytes(32)
        if not isinstance(secret,bytes) or len(secret)!=32:raise ValueError('Sampling secret must be 32 bytes')
        seeds[stage]={'secret':store.put_named('private-seeds/'+sha(canonical(scope)),secret).model_dump(mode='json'),
                      'commitment':seed_commitment(secret,scope,sha(canonical(packet)),policy_sha256)}
    result={'source_packet':store.put(packet).model_dump(mode='json'),'source_frame':store.put(frame).model_dump(mode='json'),
            'seeds_by_stage':seeds,'document_uid_by_source_ordinal':{'S'+str(i+1).zfill(3):d.document_uid for i,d in enumerate(packet.documents) if d.file is not None},
            'source_preparation_runs':[r.model_dump(mode='json') for r in operations.run_refs]}
    ref=store.put(result)
    # Once committed, recovery loads this artifact instead of drawing a different secret.
    try:
        store.advance('prepared-source:'+packet.round_id+':'+packet.paper_id,None,ref.sha256)
    except Conflict:
        # Concurrent preparation must use the authoritative winner, not replace its seeds.
        import json
        from .contracts import FileRef
        head=store.head('prepared-source:'+packet.round_id+':'+packet.paper_id)
        raw=store.get_blob(head)
        saved=json.loads(raw)
        if saved['source_packet']['sha256']!=sha(canonical(packet)):
            raise ValueError('Concurrent prepared-source binding differs')
        for stage in stages:
            seed=saved['seeds_by_stage'][stage]
            secret=store.read(FileRef.model_validate(seed['secret'],strict=True))
            if seed['commitment']!=seed_commitment(secret,(PROTOCOL,packet.round_id,stage,packet.paper_id),sha(canonical(packet)),policy_sha256):
                raise ValueError('Concurrent prepared-source policy differs')
        return saved,FileRef(relative_path='blobs/'+head,sha256=head,byte_length=len(raw),media_type='application/octet-stream')
    return result,ref
