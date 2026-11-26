"""Provider-free verification of immutable scientific evidence, samples and exact scores."""
from pathlib import Path
import tempfile
from .contracts import (SampledSilverReceipt,SourcePacket,SourceFrame,SubmittedPackage,SampleManifest,
    TaskCard,AssertionPacket,ScientificVerdict,JudgeResponse,ComparisonPlan,SourceGridManifest)
from .store import canonical,sha
from .sampling import samples,audit_buckets,seed_commitment,PROTOCOL
from .scoring import score
from .source import validate_frame
from .packets import Index, CONTEXT_HEADER_PROPERTIES, context_property_names
from .packages import Package
from .judging import validate_judgment


def replay(store,ref,*,error_registry=None):
    receipt=store.load(ref,SampledSilverReceipt)
    packet=store.load(receipt.source_packet,SourcePacket)
    source=store.load(receipt.source_frame,SourceFrame)
    plan=store.load(receipt.comparison_plan,ComparisonPlan)
    grid=store.load(plan.source_grid,SourceGridManifest)
    if plan.stage!=receipt.binding.stage or plan.source_grid_revision!=grid.revision:raise ValueError('Comparison plan/grid binding mismatch')
    validate_frame(source,packet)
    from .docling_frame import validate_docling_frame
    docling_evidence=validate_docling_frame(store,source,packet)
    if docling_evidence:
        from .source_recovery import validate_frame_scope
        validate_frame_scope(store,source,receipt.binding)
    if any(d.file and d.file.media_type=='application/pdf' for d in packet.documents) and not docling_evidence:
        raise ValueError('PDF replay requires a bound independent Docling source frame')
    packages=[store.load(r,SubmittedPackage) for r in receipt.input_packages]
    if receipt.source_packet.sha256!=receipt.binding.source_packet_sha256 or receipt.source_frame.sha256!=receipt.binding.source_frame_sha256:
        raise ValueError('Original source binding changed')
    aliases=[p.output_alias for p in packages]
    active={plan.baseline_alias,*plan.challenger_aliases}
    if len(aliases)!=len(set(aliases)) or not active<={*aliases} or plan.reference_alias not in aliases:
        raise ValueError('Incomplete sealed package pool')
    if {g.output_alias for g in receipt.deterministic_gates}!=active or len(receipt.deterministic_gates)!=len(active):
        raise ValueError('Incomplete or duplicate mechanical gates')
    if {v.output_alias for v in receipt.paper_scores}!=active or len(receipt.paper_scores)!=len(active):
        raise ValueError('Incomplete or duplicate common score grid')
    for p in packages:
        if (p.round_id,p.paper_id,p.source_packet_sha256)!=(receipt.binding.round_id,receipt.binding.paper_id,receipt.binding.source_packet_sha256):
            raise ValueError('Submitted package scope changed')
    if error_registry is None:
        from .errors import validate_error, BUDGETED_HOLD_CODE
    else:
        validate_error = error_registry.validate_error
        BUDGETED_HOLD_CODE = error_registry.BUDGETED_HOLD_CODE
    for error in [*receipt.hold_reasons,*(e for g in receipt.deterministic_gates for e in g.errors),*(e for p in packages for e in p.errors)]:
        validate_error(error)
    experimental_hold = any(error.code == BUDGETED_HOLD_CODE for error in receipt.hold_reasons)
    if experimental_hold and receipt.state != 'HOLD':
        raise ValueError('Experimental budgeted receipts must remain HOLD')
    pool=sha(canonical(sorted((p.model_dump(mode='json') for p in packages),key=lambda p:p['output_alias'])))
    if pool!=receipt.binding.input_pool_sha256:raise ValueError('Original package pool changed')
    for f in [receipt.source_frame,receipt.source_packet,receipt.comparison_plan,receipt.sampling_manifest,*receipt.input_packages,*receipt.task_cards,*receipt.assertion_packets,*receipt.judge_runs,*receipt.scientific_verdicts]:store.read(f)
    for d in packet.documents:
        if d.file is not None:store.read(d.file)
    for table in source.tables:
        if table.layout_receipt is not None:
            from .layout_validation import validate_layout
            validate_layout(store,table,source,packet)
    for anchor in source.anchors:
        if anchor.rendering is not None:store.read(anchor.rendering)
    manifest=store.load(receipt.sampling_manifest,SampleManifest)
    if manifest.binding!=receipt.binding:raise ValueError('Sample binding mismatch')
    secret=bytes.fromhex(manifest.revealed_seed_hex)
    scope=(PROTOCOL,receipt.binding.round_id,receipt.binding.stage,receipt.binding.paper_id)
    if seed_commitment(secret,scope,receipt.binding.source_packet_sha256,receipt.binding.policy_sha256)!=manifest.seed_commitment:raise ValueError('Invalid revealed paper secret')
    selected=samples(source.tiles,secret,scope)
    streams={'CONTENT_PROSE':'content_prose','CONTENT_TABLE':'content_table','ARCHIVE':'archive'}
    for selection in manifest.selections:
        if selection.stream in streams and selection.selected_ids!=selected[streams[selection.stream]]:raise ValueError('Sample codec mismatch')
    cards={}
    for r in receipt.task_cards:
        card=store.load(r,TaskCard)
        if card.binding!=receipt.binding or card.task_id in cards:raise ValueError('Task identity mismatch')
        cards[card.task_id]=card
    packets={};packet_refs={}
    for r in receipt.assertion_packets:
        case=store.load(r,AssertionPacket)
        if case.binding!=receipt.binding or case.task_card_sha256!=sha(canonical(cards[case.task_id])):raise ValueError('Assertion packet binding mismatch')
        if r.sha256 in packets:
            # Experimental recovery can retain the same immutable packet more
            # than once. Replay its content once, preserving the original ledger
            # and the separate case/verdict checks that prevent duplicate credit.
            if experimental_hold and packet_refs[r.sha256] == r:
                continue
            raise ValueError('Duplicate packet')
        packets[r.sha256]=case
        packet_refs[r.sha256]=r
    verdicts={}
    for r in receipt.scientific_verdicts:
        verdict=store.load(r,ScientificVerdict);case=packets[verdict.packet_sha256]
        if verdict.task_id!=case.task_id or verdict.output_alias!=case.output_alias:raise ValueError('Verdict/case mismatch')
        if verdict.state!='HOLD':
            response=JudgeResponse.model_validate_json(store.get_blob(verdict.decisive_response_sha256),strict=True)
            if response.checks!=verdict.dimension_checks:raise ValueError('Verdict changed decisive judgment')
        verdicts[r.sha256]=verdict
    for package in packages:
        store.read(package.execution_receipt)
        for original_file in [package.extraction_json,*package.table_csv_files]:
            if original_file is not None:store.read(original_file)
        if package.state!='COMPLETED':continue
        with tempfile.TemporaryDirectory(prefix='claims-replay-') as work:
            root=Path(work);(root/'output.json').write_bytes(store.read(package.extraction_json))
            for f in package.table_csv_files:(root/Path(f.relative_path).name).write_bytes(store.read(f))
            original=Package(root,package.output_alias,paper_id=packet.paper_id)
            original_gate=next((g for g in receipt.deterministic_gates if g.output_alias==package.output_alias),None)
            if original_gate is None:raise ValueError('Missing package gate')
            if not original.gate.gate_bit and original_gate.gate_bit!=0:raise ValueError('Mechanical failure was waived in stored receipt')
            if not original.gate.gate_bit:continue
            index=Index(original)
            original_objects={o.object_id:o for o in index.objects}
            original_relations={canonical(r.original_ref):r for r in index.relations}
            original_closures={c.result_id:c for c in index.closures}
            available=sorted(index.components,key=lambda key:key.encode())
            for case in packets.values():
                if case.output_alias!=package.output_alias:continue
                if case.submitted_package_sha256!=sha(canonical(package)):raise ValueError('Case package hash mismatch')
                if case.match.task_id!=case.task_id or case.match.output_alias!=case.output_alias:raise ValueError('Correspondence task/alias binding changed')
                coverage=case.match.coverage
                if coverage.index_sha256!=index.sha256:raise ValueError('Correspondence index changed')
                if __import__('json').loads(store.read(coverage.available_component_ids))!=available or __import__('json').loads(store.read(coverage.inspected_component_ids))!=available:
                    raise ValueError('Certified absence lacks complete immutable search manifests')
                if not coverage.exhaustive_fallback_completed or coverage.available_components!=len(available) or coverage.inspected_components!=len(available):raise ValueError('Scientific lookup coverage is incomplete')
                if len({o.object_id for o in case.objects})!=len(case.objects):raise ValueError('Physical assertion duplicated in a packet')
                for obj in case.objects:
                    if obj!=original_objects.get(obj.object_id):raise ValueError('Original properties, presence or scoped context were omitted or rewritten')
                for header in case.context_headers:
                    task=cards[case.task_id]
                    if task.task_kind!='CONTENT' or task.scope_kind!='SOURCE_CONTEXT':
                        focal=[*case.match.candidate_refs,*(r for m in case.match.matches for r in m.output_refs)]
                        needed={key for c in case.closures for key in [c.result_id,*c.recomputed_member_ids,*c.recomputed_reference_ids]}
                        needed.update(key for e in case.relations for key in (e.source_id,e.target_id))
                        if header.kind!='RESULT' or header.object_id in needed or any(r.kind=='json' and (r.pointer==header.original_ref.pointer or r.pointer.startswith(header.original_ref.pointer+'/')) for r in focal):
                            raise ValueError('A focal scientific assertion was replaced with a partial header')
                        family=index.originals[header.object_id][1].get('group_id')
                        if not any(o.kind=='FAMILY' and o.object_id==family for o in case.objects):raise ValueError('Unscoped partial family-member header')
                    original_header=original_objects.get(header.object_id)
                    names=CONTEXT_HEADER_PROPERTIES
                    if task.task_kind=='CONTENT' and task.scope_kind=='SOURCE_CONTEXT':
                        focal=[*case.match.candidate_refs,*(r for m in case.match.matches for r in m.output_refs)]
                        prefix=header.original_ref.pointer+'/'
                        if any(r.kind=='json' and (r.pointer==header.original_ref.pointer or r.pointer==prefix+'value' or r.pointer.startswith(prefix+'value/')) for r in focal):
                            raise ValueError('A focal quantity/root was replaced with a partial context header')
                        names=context_property_names(header,focal)
                    if original_header is None or header!=original_header.model_copy(update={'properties':[p for p in original_header.properties if p.name in names]}):
                        raise ValueError('Original context header was changed or includes an unscoped value')
                for relation in case.relations:
                    if relation!=original_relations.get(canonical(relation.original_ref)):raise ValueError('Original relationship was changed')
                for closure in case.closures:
                    if closure!=original_closures.get(closure.result_id):raise ValueError('Computed/submitted closure view was changed')
                for block in case.csv_blocks:
                    table=next(t for t in original.payload['tables'] if t['id']==block.table_id)
                    rows=original.csv[block.table_id]
                    if block.filename!=table['csv_file'] or block.file_sha256!=sha(original.files[block.filename]) or block.header!=rows[0] or block.rows!=rows[block.first_row:block.first_row+len(block.rows)]:
                        raise ValueError('Original CSV display strings/record coordinates were rewritten')
                for obj in case.objects:
                    index.resolve(obj.original_ref)
                    for prop in obj.properties:
                        if prop.present:
                            index.resolve(prop.original_ref)
                            if prop.value_json!=index.doc.literal(prop.original_ref.pointer):raise ValueError('Original literal was rewritten')
                for relation in case.relations:index.resolve(relation.original_ref)
                for verdict in verdicts.values():
                    if verdict.packet_sha256==sha(canonical(case)) and verdict.state!='HOLD':
                        response=JudgeResponse.model_validate_json(store.get_blob(verdict.decisive_response_sha256),strict=True)
                        validate_judgment(response,cards[case.task_id],case,{a.anchor_id for a in source.anchors},index.resolve)
    from .table_count_review import FAILURE_PREFIX,replay_source_count
    try:row_review_policy=__import__('json').loads(store.get_blob(source.parser_config_sha256)).get('source_row_count_review_policy')
    except (KeyError,ValueError):row_review_policy=None
    for gate in receipt.deterministic_gates:
        for finding in gate.errors:
            legacy_prefix='Delivered data-row count differs from the trusted source table: '
            prefix=FAILURE_PREFIX if finding.message.startswith(FAILURE_PREFIX) else legacy_prefix if row_review_policy=='source_row_count_review_v1' and finding.message.startswith(legacy_prefix) else None
            if finding.code=='CSV_FORMAT_OR_DIMENSIONS' and prefix is not None:
                uid=finding.message[len(prefix):]
                table=next((t for t in source.tables if t.table_uid==uid),None)
                if table is None:raise ValueError('Verified row-count finding has no source table')
                replay_source_count(store,receipt,source,table)
    gates={g.output_alias:g for g in receipt.deterministic_gates}
    if receipt.state=='FROZEN':
        from .replay_checks import check_selections,check_runs
        check_selections(store,receipt,source,manifest,selected,secret,scope)
        check_runs(store,receipt,cards,packets,verdicts,secret,scope)
        identity_runs={};identity_requests={}
        from .contracts import JudgeRunReceipt
        for ref in receipt.judge_runs:
            run=store.load(ref,JudgeRunReceipt);identity_runs[run.run_id]=run
            identity_requests[run.run_id]=__import__('json').loads(store.read(run.request))
        for case in packets.values():
            if case.match.identity_receipt is None:continue
            original_package=next(p for p in packages if p.output_alias==case.output_alias)
            with tempfile.TemporaryDirectory(prefix='claims-identity-replay-') as work:
                root=Path(work);(root/'output.json').write_bytes(store.read(original_package.extraction_json))
                for f in original_package.table_csv_files:(root/Path(f.relative_path).name).write_bytes(store.read(f))
                from .match_identity import replay_identity
                from .sharding import parent_id
                replay_identity(store,case,cards[case.task_id],Index(Package(root,case.output_alias)),source,
                    identity_runs,identity_requests,cards[parent_id(case.task_id)])
        for alias in active:
            if gates[alias].gate_bit!=1:continue
            from .sharding import leaves,validate_child,parent_id,MARKER
            own_ids={p.task_id for p in packets.values() if p.output_alias==alias}
            own=[c for c in cards.values() if (MARKER not in c.task_id and c.output_alias in {None,alias}) or c.task_id in own_ids]
            for c in own:
                if MARKER in c.task_id:validate_child(c,cards[parent_id(c.task_id)])
            expected={c.task_id for c in leaves(own)}
            vector=next(v for v in receipt.paper_scores if v.output_alias==alias)
            if {b.task_id for b in vector.task_bits}!=expected:
                raise ValueError('Required scientific cards or bucket checks were dropped')
            from collections import Counter
            audited={}
            for bit in vector.task_bits:
                task=cards[bit.task_id]
                if task.task_kind!='OUTPUT_AUDIT':continue
                case=packets[verdicts[bit.verdict_sha256].packet_sha256]
                audited.setdefault(task.source_bucket_id,Counter()).update(canonical(r) for r in case.match.candidate_refs)
            for bucket in manifest.output_buckets:
                if bucket.output_alias!=alias or bucket.bucket_id not in audited:continue
                if audited[bucket.bucket_id]!=Counter(canonical(r) for r in bucket.assertion_refs):
                    raise ValueError('Audit shards dropped, duplicated or invented ownership references')
    elif receipt.state=='HOLD' and (not receipt.hold_reasons or any(v.score is not None for v in receipt.paper_scores)):
        raise ValueError('Common hold acquired numeric scores')
    for vector in receipt.paper_scores:
        own={**selected,'output_audit':audit_buckets([b.bucket_id for b in manifest.output_buckets if b.output_alias==vector.output_alias],secret,scope)}
        for bit in vector.task_bits:
            verdict=verdicts[bit.verdict_sha256]
            if (verdict.output_alias!=vector.output_alias or verdict.task_id!=bit.task_id or bit.bit!=int(verdict.state=='PASS') or verdict.state=='HOLD'):raise ValueError('Invented scientific bit')
        recomputed=score(receipt.binding,vector.output_alias,gates[vector.output_alias].gate_bit,source,own,vector.task_bits,hold=receipt.state=='HOLD')
        if vector!=recomputed:raise ValueError('Exact scientific score changed on replay')
    return receipt
