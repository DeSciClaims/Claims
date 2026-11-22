"""Conservative, source-grounded identity adjudication for oversized C lookups."""
from concurrent.futures import ThreadPoolExecutor
from .contracts import MatchIdentityReceipt, IdentityReviewResponse, MatchResult, RequirementMatch
from .store import canonical
from .transport import judge_evidence, audit_evidence
from .budgets import SOURCE_TOKEN_BUDGETS
from .operations import Hold


def owner_groups(index, match):
    """A full scientific owner stays indivisible, including its attached errors."""
    groups={}
    for ref in match.candidate_refs:
        if ref.kind!='json':raise Hold('EVALUATOR','Scientific identity review needs original JSON references')
        pointer=ref.pointer
        if pointer.startswith(('/results/','/cross_group_summaries/')):
            root='/'.join(pointer.split('/')[:3])
        elif pointer.startswith('/relationships/'):
            edge=next(e for e in index.relations if pointer==e.original_ref.pointer or pointer.startswith(e.original_ref.pointer+'/'))
            owners={'/'.join(index.originals[key][0].split('/')[:3]) for key in (edge.source_id,edge.target_id)
                if key in index.originals and index.originals[key][0].startswith(('/results/','/cross_group_summaries/'))}
            root=next(iter(owners)) if len(owners)==1 else edge.original_ref.pointer
        else:
            root=next(k for k in sorted(index.components,key=len,reverse=True) if pointer==k or pointer.startswith(k+'/'))
        groups.setdefault(root,{})[canonical(ref)]=ref
    return {key:list(groups[key].values()) for key in sorted(groups,key=lambda x:x.encode())}


def group_requirements(match, groups):
    owner={canonical(ref):key for key,refs in groups.items() for ref in refs}
    return {m.requirement_id:sorted({owner[canonical(r)] for r in m.output_refs},key=lambda x:x.encode()) for m in match.matches}


def scientific_owner(index, root):
    # Scientific fields remain scientific regardless of their storage section.
    obj=next((o for o in index.objects if o.original_ref.pointer==root),None)
    if obj is not None and obj.kind=='FIELD':return True
    node=index.doc.resolve(root)
    if isinstance(node,dict) and node.get('fields'):return True
    if root.startswith(('/results/','/cross_group_summaries/')):return True
    edge=next((e for e in index.relations if e.original_ref.pointer==root),None)
    return edge is not None and edge.relation not in {'applies_under','table_for'}


def validate_review(response, task, alias, groups, requirements, anchors, scientific):
    if response.task_id!=task.task_id or response.output_alias!=alias:raise ValueError('Wrong identity review binding')
    if response.inspected_owner_ids!=list(groups):raise ValueError('Incomplete identity-owner inspection')
    if set(response.decisions)!=set(requirements):raise ValueError('Incomplete identity requirement map')
    for key,owners in requirements.items():
        if set(response.decisions[key])!=set(owners):raise ValueError('Every proposed owner needs an explicit identity decision')
        for owner,decision in response.decisions[key].items():
            if not set(decision.evidence_anchor_ids)<=anchors:raise ValueError('Unknown source identity witness')
            if owner in scientific and decision.disposition=='CONTEXT_ONLY':
                raise ValueError('A scientific owner cannot be excluded as mere context or representation format')


def apply_reviews(match, groups, reviews):
    """Exclusion requires matching positive dispositions from BOTH independent roles."""
    owner={canonical(ref):key for key,refs in groups.items() for ref in refs}
    matches=[]
    for item in match.matches:
        refs=[]
        for ref in item.output_refs:
            decisions=[r.decisions[item.requirement_id][owner[canonical(ref)]] for r in reviews]
            exclude=(decisions[0].disposition==decisions[1].disposition and decisions[0].disposition!='KEEP')
            if not exclude:refs.append(ref)
        matches.append(RequirementMatch(requirement_id=item.requirement_id,output_refs=refs))
    candidates=list({canonical(ref):ref for m in matches for ref in m.output_refs}.values())
    return match.model_copy(update={'matches':matches,'candidate_refs':candidates,
        'state':match.state if candidates else 'ABSENT','identity_receipt':None})


def owner_ledger(index, groups):
    return {root:{'original_ref':{'kind':'json','output_alias':index.alias,'pointer':root},
        'full_original_json':index.doc.literal(root),'context_headers':index.match_context([root])} for root in groups}


def merge_reviews(task, alias, groups, requirements, responses):
    merged={key:{} for key in requirements};seen=set()
    for response in responses:
        owners=set(response.inspected_owner_ids)
        if seen&owners:raise ValueError('Identity owner was reviewed twice by one role')
        seen.update(owners)
        for key,decisions in response.decisions.items():merged[key].update(decisions)
    if seen!=set(groups) or any(set(merged[key])!=set(roots) for key,roots in requirements.items()):
        raise ValueError('Independent identity review left an owner unresolved')
    return IdentityReviewResponse(task_id=task.task_id,output_alias=alias,inspected_owner_ids=list(groups),decisions=merged)


def refine_identity(index, ops, task, match, frame):
    groups=owner_groups(index,match);requirements=group_requirements(match,groups)
    scientific={root for root in groups if scientific_owner(index,root)}
    source=judge_evidence({'anchors':[a.model_dump(mode='json') for a in frame.anchors]})
    if max(t(canonical(source).decode()) for t in ops.tokenizers.values())>SOURCE_TOKEN_BUDGETS[0]:
        needed=set(task.evidence_anchor_ids)
        source=judge_evidence({'anchors':[a.model_dump(mode='json') for a in frame.anchors if a.anchor_id in needed]})
    anchors={a['anchor_id'] for a in source['anchors']}
    data={'task':task.model_dump(mode='json'),'output_alias':index.alias,
        'owner_groups':{root:[r.model_dump(mode='json') for r in refs] for root,refs in groups.items()},
        'scientific_owner_ids':sorted(scientific),'required_owner_ids_by_requirement':requirements,
        'inspection_owner_ids':list(groups),
        'owners':owner_ledger(index,groups),
        'authoritative_evidence':source}
    roots=list(groups);batches=[roots[i:i+4] for i in range(0,len(roots),4)]
    work=[]
    for role in ('SECONDARY','ARBITER'):
        for batch in batches:
            subset={root:groups[root] for root in batch}
            request={**data,'owner_groups':{root:data['owner_groups'][root] for root in batch},
                'owners':{root:data['owners'][root] for root in batch},'inspection_owner_ids':batch,
                'scientific_owner_ids':sorted(scientific&set(batch)),
                'required_owner_ids_by_requirement':{key:[r for r in owners if r in subset] for key,owners in requirements.items()}}
            work.append((role,ops.scoped(),request,subset))
    def run(item):
        role,child,request,subset=item
        return child.request(role,'source_match_identity',request,IdentityReviewResponse,
            validate=lambda response:validate_review(response,task,index.alias,subset,request['required_owner_ids_by_requirement'],anchors,scientific&set(subset)))
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(run,work))
    finally:
        for _,child,_,_ in work:
            ops.used.extend(child.used);ops.run_refs.extend(child.run_refs)
            ops.packet_refs.extend(child.packet_refs);ops.response_refs.update(child.response_refs)
    responses=[merge_reviews(task,index.alias,groups,requirements,[response for item,response in zip(work,results) if item[0]==role]) for role in ('SECONDARY','ARBITER')]
    refined=apply_reviews(match,groups,responses)
    receipt=MatchIdentityReceipt(task_id=task.task_id,output_alias=index.alias,original_match=ops.store.put(match),
        refined_match=ops.store.put(refined),owner_groups=groups,authoritative_evidence=ops.store.put(source),
        judge_run_ids={role:[rid for r,child,_,_ in work if r==role for rid in child.used] for role in ('SECONDARY','ARBITER')})
    return refined.model_copy(update={'identity_receipt':ops.store.put(receipt)})


def replay_identity(store, case, task, index, frame, runs, requests, parent_task):
    from .sharding import parent_id
    receipt=store.load(case.match.identity_receipt,MatchIdentityReceipt)
    if receipt.task_id!=parent_id(task.task_id) or receipt.output_alias!=case.output_alias:
        raise ValueError('Identity receipt changed source/card binding')
    original=store.load(receipt.original_match,MatchResult)
    if original.identity_receipt is not None or original.task_id!=receipt.task_id or original.output_alias!=receipt.output_alias:
        raise ValueError('Identity refinement changed its original lookup binding')
    proposed={m.requirement_id:set() for m in original.matches};inspected=set()
    for rid,run in runs.items():
        request=requests[rid];data=request['payload']
        if run.error or not run.parsed_response or request['operation']!='source_match' or request['role']!='PRIMARY' or data.get('task',{}).get('task_id')!=receipt.task_id or data.get('output_alias')!=receipt.output_alias:continue
        from .contracts import MatchResponse
        response=store.load(run.parsed_response,MatchResponse);inspected.update(response.inspected_component_ids)
        for item in response.matches:
            if item.requirement_id not in proposed:raise ValueError('Initial lookup changed its source requirement')
            proposed[item.requirement_id].update(canonical(r) for r in item.output_refs)
    if inspected!=set(index.components) or proposed!={m.requirement_id:{canonical(r) for r in m.output_refs} for m in original.matches}:
        raise ValueError('Identity refinement lost original exhaustive lookup proposals')
    groups=owner_groups(index,original)
    if groups!=receipt.owner_groups:raise ValueError('Identity review dropped or rewrote proposed candidates')
    requirements=group_requirements(original,groups);scientific={r for r in groups if scientific_owner(index,r)}
    evidence=__import__('json').loads(store.read(receipt.authoritative_evidence));decoded=audit_evidence(evidence)
    source={a.anchor_id:a.model_dump(mode='json') for a in frame.anchors}
    if any(a!=source.get(a['anchor_id']) for a in decoded['anchors']):raise ValueError('Identity review changed source evidence')
    anchors={a['anchor_id'] for a in decoded['anchors']};responses=[];identities=[]
    parent=parent_task
    if parent.task_id!=receipt.task_id:raise ValueError('Identity parent task is absent')
    if not set(parent.evidence_anchor_ids)<=anchors:raise ValueError('Identity review omitted original source-task evidence')
    if set(receipt.judge_run_ids)!={'SECONDARY','ARBITER'}:raise ValueError('Identity exclusion needs two independent reviews')
    for role,ids in receipt.judge_run_ids.items():
        accepted=[];role_identities=set()
        for rid in ids:
            if rid not in runs:raise ValueError('Identity review run is absent')
            run=runs[rid];request=requests[rid];data=request['payload']
            if run.role!=role or request['operation']!='source_match_identity' or data['task']!=parent.model_dump(mode='json') or data['output_alias']!=receipt.output_alias:
                raise ValueError('Identity review role/task changed')
            batch=data['inspection_owner_ids']
            if len(batch)!=len(set(batch)) or not set(batch)<=set(groups):raise ValueError('Unknown or duplicated identity-owner batch')
            subset={root:groups[root] for root in batch};expected_requirements={key:[r for r in owners if r in subset] for key,owners in requirements.items()}
            if data['owner_groups']!={k:[r.model_dump(mode='json') for r in v] for k,v in subset.items()} or data['authoritative_evidence']!=evidence:
                raise ValueError('Independent identity review inspected different originals/source')
            if data['required_owner_ids_by_requirement']!=expected_requirements or data['owners']!=owner_ledger(index,subset) or data['scientific_owner_ids']!=sorted(scientific&set(subset)):
                raise ValueError('Identity owner or its original scope headers were rewritten')
            if not run.error and run.parsed_response:
                response=store.load(run.parsed_response,IdentityReviewResponse)
                validate_review(response,parent,index.alias,subset,expected_requirements,anchors,scientific&set(subset))
                accepted.append(response);role_identities.add((request['route']['provider'],request['route']['model'],request['route']['runtime_revision']))
        if len(role_identities)!=1:raise ValueError('Identity role changed model/runtime')
        identities.extend(role_identities)
        responses.append(merge_reviews(parent,index.alias,groups,requirements,accepted))
    if len(set(identities))!=2:raise ValueError('Identity reviews share a model/runtime')
    expected=apply_reviews(original,groups,responses)
    if expected!=store.load(receipt.refined_match,MatchResult):raise ValueError('Identity exclusion is not the conservative review intersection')
    ids={r.requirement_id for r in task.requirements}
    matches=[m for m in expected.matches if m.requirement_id in ids]
    candidates=list({canonical(r):r for m in matches for r in m.output_refs}.values())
    expected=expected.model_copy(update={'task_id':task.task_id,'matches':matches,'candidate_refs':candidates,
        'state':expected.state if candidates else 'ABSENT','identity_receipt':case.match.identity_receipt})
    if case.match!=expected:raise ValueError('A shard dropped identity-scoped candidates or lost review provenance')
