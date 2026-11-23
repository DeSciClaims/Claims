"""Complete independent shards, with one binary scientific bit per original card/bucket."""
from .budgets import STRUCTURED_PACKET_TOKENS
from .store import canonical,sha
from .operations import Hold
from .transport import judge_packet

MARKER='::shard:'


def parent_id(task_id):
    return task_id.split(MARKER,1)[0]


def leaves(cards):
    parents={parent_id(c.task_id) for c in cards if MARKER in c.task_id}
    return [c for c in cards if c.task_id not in parents]


def validate_child(child,parent):
    if not child.task_id.startswith(parent.task_id+MARKER) or child.binding!=parent.binding:
        raise ValueError('Shard changed its original task binding')
    for name in ('task_kind','scope_kind','tile_ids','output_alias','source_bucket_id'):
        if getattr(child,name)!=getattr(parent,name):raise ValueError('Shard changed source/card identity')
    if child.source_scope_note!=parent.source_scope_note and not any(child.source_scope_note.startswith(parent.source_scope_note+prefix) for prefix in (' Controller archival shard owns ',' Controller audit shard owns ')):
        raise ValueError('Shard rewrote its source scope')
    if not set(child.evidence_anchor_ids)<=set(parent.evidence_anchor_ids):raise ValueError('Shard invented evidence')
    requirements={r.requirement_id:r for r in parent.requirements}
    for r in child.requirements:
        original=requirements.get(r.requirement_id)
        if original is None or r.statement!=original.statement or r.qualifications!=original.qualifications or r.encoding_constraints!=original.encoding_constraints or r.analysis_scope!=original.analysis_scope or r.adjustment_scope!=original.adjustment_scope:
            raise ValueError('Shard rewrote a scientific requirement')
        if not set(r.evidence_anchor_ids)<=set(original.evidence_anchor_ids):raise ValueError('Shard invented a requirement witness')


def partition(index,task,packet,*,tokenizers,owned_refs=None,table_ranges=None,source_frame=None):
    def size(p):return max(f(canonical(judge_packet(p)).decode()) for f in tokenizers.values())
    output_fits=size(packet)<=STRUCTURED_PACKET_TOKENS
    from .evidence_batching import source_size
    from .budgets import SOURCE_TOKEN_BUDGETS
    source_fits=source_frame is None or source_size(source_frame,task.evidence_anchor_ids,tokenizers)<=SOURCE_TOKEN_BUDGETS[0]
    if output_fits and source_fits:return [(task,packet)]
    groups=[]
    if task.task_kind=='CONTENT' and task.scope_kind=='SOURCE_CONTEXT':
        # Source-only reviewers distinguish conventions/conditions from findings.
        # Each unchanged context requirement remains complete, with all its original
        # representations. Reusable context is repeated, not used to join every
        # unrelated result that happens to share the same protocol or convention.
        groups=[([r],None,None,[]) for r in task.requirements]
    elif task.task_kind=='CONTENT':
        assigned={canonical(r) for m in packet.match.matches for r in m.output_refs}
        extras=[r for r in packet.match.candidate_refs if canonical(r) not in assigned]
        # Each unchanged source requirement is a complete semantic target. Repeat
        # its FULL objects, graph endpoints, closures and all conflicting views.
        # Shared assertions are duplicated, never cut apart or awarded twice;
        # every required shard must pass for the single original parent bit.
        groups=[([r],None,None,extras) for r in task.requirements]
    elif task.task_kind=='OUTPUT_AUDIT':
        if owned_refs is None:raise ValueError('Missing complete audit ownership ledger')
        from .audit_groups import assertion_groups
        groups=[([],refs,None,[]) for refs in assertion_groups(index,owned_refs)]
    else:
        for table,(first,stop) in sorted((table_ranges or {}).items()):
            for row in range(first,stop):groups.append((task.requirements,None,{table:(row,row+1)},None))
    if len(groups)<2 and output_fits:return [(task,packet)]  # Stream a genuinely joint source scope without cutting dependencies.
    if len(groups)<2:
        raise Hold('EVALUATOR','No complete independent partition fits the 32000-token bound')
    shards=[]
    for ordinal,(requirements,refs,ranges,extras) in enumerate(groups):
        child=task.model_copy(update={'task_id':task.task_id+MARKER+sha(index.alias.encode())[:16]+'-'+str(ordinal),'requirements':requirements})
        match=packet.match
        if task.task_kind=='CONTENT':
            all_required={a for r in task.requirements for a in r.evidence_anchor_ids}
            shared=set(task.evidence_anchor_ids)-all_required
            relevant=shared|{a for r in requirements for a in r.evidence_anchor_ids}
            child=child.model_copy(update={'evidence_anchor_ids':[a for a in task.evidence_anchor_ids if a in relevant]})
            ids={r.requirement_id for r in requirements}
            matches=[m for m in match.matches if m.requirement_id in ids]
            candidates=list({canonical(r):r for r in [*extras,*(r for m in matches for r in m.output_refs)]}.values())
            match=match.model_copy(update={'matches':matches,'candidate_refs':candidates,'state':match.state if candidates else 'ABSENT'})
        elif task.task_kind=='OUTPUT_AUDIT':
            # Ownership, not correspondence, determines which assertions this shard checks.
            match=match.model_copy(update={'candidate_refs':refs,'matches':[],'state':'MATCHED' if refs else 'ABSENT'})
            child=child.model_copy(update={'source_scope_note':task.source_scope_note+
                ' Controller audit shard owns only packet.match.candidate_refs in this packet. Other parent-bucket assertions are checked in separate mandatory shards. Other rendered objects/relations provide context, not additional ownership.'})
        else:
            # Every archival shard keeps the complete source block as evidence; its
            # literal scope tells the judge exactly which original CSV records it owns.
            if source_frame is None:raise Hold('EVALUATOR','Archival shard lacks independent source-row mapping')
            tile=next(t for t in source_frame.tiles if t.tile_id==task.tile_ids[0])
            source_table=next(t for t in source_frame.tables if t.table_uid==tile.table_uid)
            first,stop=next(iter(ranges.values()))
            ids=set(source_table.data_row_anchor_ids[first:stop]+tile.context_anchor_ids)
            evidence=[aid for aid in task.evidence_anchor_ids if aid in ids]
            child=child.model_copy(update={'evidence_anchor_ids':evidence,
                'requirements':[r.model_copy(update={'evidence_anchor_ids':[a for a in r.evidence_anchor_ids if a in ids]}) for r in requirements],
                'source_scope_note':task.source_scope_note+' Controller archival shard owns source rows '+str(first+1)+' through '+str(stop)+'. Other source rows are checked in separate mandatory shards; assess only these owned source rows with their complete context.'})
        match=match.model_copy(update={'task_id':child.task_id})
        validate_child(child,task)
        rendered=index.packet(child,child.binding,match,packet.submitted_package_sha256,packet.document_bindings,
            owned_refs=refs,table_ranges=ranges or table_ranges)
        if size(rendered)>STRUCTURED_PACKET_TOKENS:
            raise Hold('EVALUATOR','A complete joint assertion group exceeds the shard bound')
        shards.append((child,rendered))
    return shards
