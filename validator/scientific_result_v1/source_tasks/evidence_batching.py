"""Bounded original-source scans; independent retrieval never becomes a score bit."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from .contracts import (EvidenceBatchResponse,EvidenceSearchQuery,EvidenceSearchScan,
    EvidenceSearchReceipt,AssertionPacket,JudgeRunReceipt,FileRef)
from .budgets import SOURCE_TOKEN_BUDGETS,STRUCTURED_PACKET_TOKENS
from .store import canonical,sha
from .transport import judge_evidence,judge_packet,audit_evidence
from .operations import Hold

ROLES=('PRIMARY','SECONDARY')


def evidence(frame,ids):
    wanted=set(ids)
    return {'anchors':[a.model_dump(mode='json') for a in frame.anchors if a.anchor_id in wanted]}


def source_size(frame,ids,tokenizers):
    wire=canonical(judge_evidence(evidence(frame,ids))).decode()
    return max(f(wire) for f in tokenizers.values())


def pack(items,size,limit):
    """Stable disjoint complete batches; a single oversized item is never truncated."""
    items=list(items);batches=[];start=0
    while start<len(items):
        if size(items[start:start+1])>limit:
            raise Hold('SOURCE','One complete source anchor or lookup query exceeds the per-request bound')
        low=start+1;high=len(items)
        while low<high:
            middle=(low+high+1)//2
            if size(items[start:middle])<=limit:low=middle
            else:high=middle-1
        batches.append(items[start:low]);start=low
    return batches


def mandatory(frame,task):
    if task.task_kind=='CONTENT':return {a for r in task.requirements for a in r.evidence_anchor_ids}
    if task.task_kind=='ARCHIVE':
        tile=next(t for t in frame.tiles if t.tile_id==task.tile_ids[0])
        table=next(t for t in frame.tables if t.table_uid==tile.table_uid)
        return set(task.evidence_anchor_ids)&set(table.data_row_anchor_ids+table.header_anchor_ids)
    return set()


def lookup_query(packet,task):
    wire=judge_packet(packet)
    # Retrieval sees exact asserted properties and dependencies, without repeating
    # administrative manifests, long source-ID lists or computed closure ledgers.
    keep={'assertion_encoding','objects','relations','context_headers','csv_blocks','document_bindings',
        'property_name_sets','literal_pool','literal_identifier_pool','literal_identifier_prefix',
        'output_reference_pool','reference_output_alias'}
    return {'query_id':task.task_id,'task_kind':task.task_kind,'scope_kind':task.scope_kind,
        'requirements':[r.model_dump(mode='json',exclude={'evidence_anchor_ids'}) for r in task.requirements],
        'ownership':wire['match']['candidate_refs'],'assertions':{k:v for k,v in wire.items() if k in keep}}


def selected(frame,scope,required,nominations):
    ids=set(required)|set(nominations)
    if not ids:
        # A document anchor supplies literal source identity, never a fabricated
        # support witness. Complete blind search receipts accompany the judge.
        first={}
        for a in frame.anchors:
            if a.anchor_id in scope:first.setdefault(a.document_uid,a.anchor_id)
        ids.update(first.values())
    return [a.anchor_id for a in frame.anchors if a.anchor_id in ids]


def prepare(ops,frame,shards):
    """Return complete assertion cases with bounded, independently retrieved evidence."""
    limit=SOURCE_TOKEN_BUDGETS[0]
    needed=[(task,packet) for task,packet in shards if source_size(frame,task.evidence_anchor_ids,ops.tokenizers)>limit]
    if not needed:return [(task,packet,evidence(frame,task.evidence_anchor_ids),None) for task,packet in shards]
    scope_set={a for task,_ in needed for a in task.evidence_anchor_ids}
    scope=[a.anchor_id for a in frame.anchors if a.anchor_id in scope_set]
    if set(scope)!=scope_set:raise ValueError('Source lookup scope contains unknown anchors')
    sb=pack(scope,lambda ids:source_size(frame,ids,ops.tokenizers),limit)
    queries={task.task_id:lookup_query(packet,task) for task,packet in needed}
    def query_size(ids):return max(f(canonical([queries[k] for k in ids]).decode()) for f in ops.tokenizers.values())
    qb=pack(list(queries),query_size,STRUCTURED_PACKET_TOKENS)
    aggregate=Lock();journals={}
    def scan(item):
        ordinal,(role,anchor_ids,query_ids)=item;local=ops.scoped()
        data={'request_id':'source-lookup-'+sha(canonical([frame.source_packet_sha256,needed[0][0].binding,role,anchor_ids,[queries[k] for k in query_ids]])),
            'inspected_anchor_ids':anchor_ids,'inspected_query_ids':query_ids,
            'queries':[queries[k] for k in query_ids],'authoritative_evidence':evidence(frame,anchor_ids)}
        def validate(response):
            if (response.request_id!=data['request_id'] or response.inspected_anchor_ids!=anchor_ids
                or response.inspected_query_ids!=query_ids or set(response.witnesses)!=set(query_ids)):
                raise ValueError('Incomplete source lookup batch or query inspection')
            for ids in response.witnesses.values():
                if len(ids)!=len(set(ids)) or not set(ids)<=set(anchor_ids):raise ValueError('Source lookup invented or duplicated a witness')
        try:
            response=local.request(role,'source_evidence_lookup',data,EvidenceBatchResponse,validate=validate,
                source_limit=limit,packet_tokens=query_size(query_ids))
        finally:
            with aggregate:
                journals[ordinal]=(list(local.run_refs),list(local.used))
        receipt=EvidenceSearchScan(role=role,query_ids=query_ids,anchor_ids=anchor_ids,
            judge_runs=list(local.run_refs),response=local.store.put(response))
        return receipt,response
    items=[(role,anchors,ids) for role in ROLES for anchors in sb for ids in qb]
    scans=[];nominees={key:set() for key in queries}
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            for scan_ref,response in executor.map(scan,enumerate(items)):
                scans.append(scan_ref)
                for key,ids in response.witnesses.items():nominees[key].update(ids)
    finally:
        # Completion order may differ on replay. Preserve stable request order,
        # including every paid/failed attempt if one batch prevents completion.
        for ordinal in sorted(journals):
            refs,used=journals[ordinal];ops.run_refs.extend(refs);ops.used.extend(used)
    choices={task.task_id:selected(frame,scope,mandatory(frame,task),nominees[task.task_id]) for task,_ in needed}
    for ids in choices.values():
        if source_size(frame,ids,ops.tokenizers)>limit:
            raise Hold('SOURCE','One complete joint source witness set exceeds the per-request bound after exhaustive batched lookup')
    search=EvidenceSearchReceipt(kind='complete_source_batch_search_v1',binding=needed[0][0].binding,scope_anchor_ids=scope,
        queries=[EvidenceSearchQuery(query_id=task.task_id,task=ops.store.put(task),original_packet=ops.store.put(packet),required_anchor_ids=sorted(mandatory(frame,task))) for task,packet in needed],
        scans=scans,selected_anchor_ids=choices)
    ref=ops.store.put(search);result=[]
    for task,packet in shards:
        if task.task_id not in choices:
            result.append((task,packet,evidence(frame,task.evidence_anchor_ids),None));continue
        ids=choices[task.task_id]
        packet=packet.model_copy(update={'evidence_anchor_ids':ids})
        packet=packet.model_copy(update={'packet_id':'packet-'+sha(canonical(packet.model_dump(exclude={'packet_id'})))})
        summary={'kind':search.kind,'query_id':task.task_id,'receipt':ref.model_dump(mode='json'),
            'complete_for_scope':True,'source_anchor_count':len(scope),'source_batch_count':len(sb),
            'independent_roles':list(ROLES),'selected_anchor_count':len(ids)}
        result.append((task,packet,evidence(frame,ids),summary))
    return result


def validate_search(store,summary,frame,case,task,runs,requests):
    """Reconstruct every blind search and the exact union of original witnesses."""
    from .contracts import TaskCard
    search=store.load(FileRef.model_validate(summary['receipt']),EvidenceSearchReceipt)
    if search.binding!=case.binding or summary['kind']!=search.kind or summary['query_id']!=task.task_id:
        raise ValueError('Source search changed scientific case binding')
    if search.binding.source_frame_sha256!=sha(canonical(frame)):raise ValueError('Source lookup changed the original source frame')
    anchors={a.anchor_id:a.model_dump(mode='json') for a in frame.anchors}
    scope=search.scope_anchor_ids
    if len(scope)!=len(set(scope)) or not set(scope)<=anchors.keys():raise ValueError('Source search scope invented or duplicated anchors')
    queries={};tasks={};originals={}
    for query in search.queries:
        if query.query_id in queries:raise ValueError('Duplicate source search query')
        original=store.load(query.original_packet,AssertionPacket);original_task=store.load(query.task,TaskCard)
        if (original.binding!=search.binding or original.task_id!=query.query_id or original_task.task_id!=query.query_id
            or original.task_card_sha256!=query.task.sha256 or original_task.binding!=search.binding
            or original.output_alias!=case.output_alias or not set(original_task.evidence_anchor_ids)<=set(scope)
            or query.required_anchor_ids!=sorted(mandatory(frame,original_task))):
            raise ValueError('Source search changed original task, packet or mandatory witness')
        queries[query.query_id]=lookup_query(original,original_task);tasks[query.query_id]=original_task;originals[query.query_id]=original
    if tasks.get(task.task_id)!=task:raise ValueError('Source search lacks the original scientific task')
    nominees={key:set() for key in queries};coverage={(role,key):[] for role in ROLES for key in queries}
    from .tokenization import BudgetTokenizer
    counters={'pinned':BudgetTokenizer()}
    identities={}
    for scan in search.scans:
        if (len(scan.anchor_ids)!=len(set(scan.anchor_ids)) or not scan.anchor_ids or not set(scan.anchor_ids)<=set(scope)
            or len(scan.query_ids)!=len(set(scan.query_ids)) or not scan.query_ids or not set(scan.query_ids)<=queries.keys()):
            raise ValueError('Invalid source search batch inventory')
        if source_size(frame,scan.anchor_ids,counters)>SOURCE_TOKEN_BUDGETS[0]:raise ValueError('Source search exceeded its source request allowance')
        if max(f(canonical([queries[k] for k in scan.query_ids]).decode()) for f in counters.values())>STRUCTURED_PACKET_TOKENS:
            raise ValueError('Source search exceeded its query request allowance')
        response=store.load(scan.response,EvidenceBatchResponse)
        if response.inspected_anchor_ids!=scan.anchor_ids or response.inspected_query_ids!=scan.query_ids or set(response.witnesses)!=set(scan.query_ids):
            raise ValueError('Incomplete independent source search inspection')
        matching=[]
        for ref in scan.judge_runs:
            run=store.load(ref,JudgeRunReceipt)
            if run.run_id not in runs or runs[run.run_id]!=run:raise ValueError('Source lookup run is outside the retained evaluation')
            request=requests[run.run_id];payload=request['payload']
            if (request['operation']!='source_evidence_lookup' or run.role!=scan.role or request['policy_sha256']!=search.binding.policy_sha256
                or payload['inspected_anchor_ids']!=scan.anchor_ids or payload['inspected_query_ids']!=scan.query_ids
                or payload['queries']!=[queries[k] for k in scan.query_ids]):raise ValueError('Source lookup changed batch, query or role')
            captured=audit_evidence(payload['authoritative_evidence'])
            if captured!=evidence(frame,scan.anchor_ids):raise ValueError('Source lookup changed original source bytes or locators')
            if not run.error and run.parsed_response==scan.response:
                if response.request_id!=payload['request_id']:raise ValueError('Source lookup response echo changed')
                matching.append(request['route'])
        if len(matching)!=1:raise ValueError('Source search lacks a successful bound provider response')
        identity=tuple(matching[0][k] for k in ('provider','model','runtime_revision'))
        if scan.role in identities and identities[scan.role]!=identity:raise ValueError('Source search changed a role runtime')
        identities[scan.role]=identity
        for key,ids in response.witnesses.items():
            if len(ids)!=len(set(ids)) or not set(ids)<=set(scan.anchor_ids):raise ValueError('Source lookup invented a witness')
            nominees[key].update(ids);coverage[(scan.role,key)].extend(scan.anchor_ids)
    if set(identities)!=set(ROLES) or len(set(identities.values()))!=2:raise ValueError('Source lookup lacks two independent model/runtime identities')
    for ids in coverage.values():
        if len(ids)!=len(scope) or set(ids)!=set(scope):raise ValueError('Source batches omitted or duplicated source coverage for a query or independent role')
    expected={q.query_id:selected(frame,scope,q.required_anchor_ids,nominees[q.query_id]) for q in search.queries}
    if search.selected_anchor_ids!=expected:raise ValueError('Source witness union omitted independent evidence or mandatory context')
    ids=expected[task.task_id]
    if source_size(frame,ids,counters)>SOURCE_TOKEN_BUDGETS[0]:raise ValueError('Joined source witnesses exceed the scientific request allowance')
    if not set(ids)<=set(case.evidence_anchor_ids):raise ValueError('Final scientific case dropped a selected source witness')
    original=originals[task.task_id]
    ignored={'packet_id','evidence_anchor_ids'}
    if original.model_dump(exclude=ignored)!=case.model_dump(exclude=ignored):raise ValueError('Source batching changed scientific assertions or dependencies')
    # Counts are derived, never a caller's claim of exhaustive coverage.
    batches={tuple(s.anchor_ids) for s in search.scans}
    expected_summary={'kind':search.kind,'query_id':task.task_id,'receipt':summary['receipt'],'complete_for_scope':True,
        'source_anchor_count':len(scope),'source_batch_count':len(batches),'independent_roles':list(ROLES),'selected_anchor_count':len(ids)}
    if summary!=expected_summary:raise ValueError('Source lookup summary changed retained coverage')
    return search
