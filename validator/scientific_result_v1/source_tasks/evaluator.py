"""One common sealed paper pool, source tasks, exact scores and retained evidence."""

from __future__ import annotations
from pathlib import Path
import tempfile
from collections import defaultdict
from .contracts import (
    EvaluationBinding,
    TaskCard,
    SourceRequirement,
    TaskBit,
    SourceBucket,
    ErrorRecord,
    SampleManifest,
    SampledSilverReceipt,
    GateReceipt,
)
from .store import canonical, sha
from .packages import Package, error
from .packets import Index
from .sampling import samples, audit_buckets, seed_commitment, selection_manifest, PROTOCOL
from .source import validate_frame, selected_cards
from .diagnostic_dispatch import worker_limit
from .judging import assess
from .scoring import score
from .operations import Hold
from .source_recovery import SourceInventoryDefect
from .errors import hold_record


def evidence_for(source_frame, ids):
    anchors = {a.anchor_id: a for a in source_frame.anchors}
    needed = set(ids)
    # Controller row anchors retain source-region references, not a guessed numerical transcript.
    # Include source context for selected rows and their complete headers/notes.
    result = []
    for anchor in source_frame.anchors:
        if anchor.anchor_id in needed:
            result.append(anchor.model_dump(mode="json"))
    return {"anchors": result}


def original_refs(index):
    return [o.original_ref for o in index.objects] + [
        r.original_ref for r in index.relations
    ]


def buckets(index, frame, document_bindings, table_uid_by_output=None):
    """Stable source occupancy from exact supplied provenance, with mandatory UNANCHORED."""
    docmap = {
        b.source_id: b.document_uid for b in document_bindings if b.state == "BOUND"
    }
    anchors = {a.anchor_id: a for a in frame.anchors}
    owner = {}
    for tile in frame.tiles:
        for aid in tile.owned_anchor_ids:
            owner.setdefault(aid, []).append(tile.tile_id)
    import unicodedata
    normalize = lambda s: " ".join(unicodedata.normalize('NFKC',s).replace('|',' ').casefold().split())
    table_uid_by_output=table_uid_by_output or {}
    source_tables={t.table_uid:t for t in frame.tables}
    output_tables={t['id']:t for t in index.package.payload['tables']}

    def direct_cells(node):
        hits=[]
        for reference in node.get('table_references',[]):
            uid=table_uid_by_output.get(reference['table_id'])
            if uid is None:continue
            table=source_tables[uid];submitted=output_tables[reference['table_id']]
            rows=index.package.csv[submitted['id']]
            row=int(reference['row'])
            if not 1<=row<len(rows):continue
            # Resolve by original row label and occurrence, not by a miner's row grouping.
            label=normalize(rows[row][0])
            if not label or not any(c.isalpha() for c in label):continue
            matches=[aid for aid in table.data_row_anchor_ids if label in normalize(anchors[aid].exact_text or '')]
            occurrence=sum(normalize(r[0])==label for r in rows[1:row])
            if occurrence<len(matches):hits.extend(owner.get(matches[occurrence],[]))
        return sorted(set(hits),key=lambda s:s.encode())

    def candidates(node):
        result = []
        snippets = node.get("source_snippets", [])
        for snippet in snippets:
            text = normalize(snippet["text"])
            doc = docmap.get(snippet["source_id"])
            hits = [
                a.anchor_id
                for a in anchors.values()
                if a.document_uid == doc
                and a.exact_text is not None
                and text
                and text in normalize(a.exact_text)
            ]
            result.extend(t for a in hits for t in owner.get(a, []))
        return sorted(set(result), key=lambda s: s.encode())

    object_bucket = {}
    owned = defaultdict(list)
    ordered = sorted(
        index.objects,
        key=lambda o: (o.kind == "FIELD", o.original_ref.pointer.encode()),
    )
    for obj in ordered:
        node = index.doc.resolve(obj.original_ref.pointer)
        options = direct_cells(node) if isinstance(node,dict) and obj.kind=='FIELD' else []
        if not options and obj.kind=='TABLE' and obj.object_id in table_uid_by_output:
            table=source_tables[table_uid_by_output[obj.object_id]]
            options=sorted({tile for aid in table.data_row_anchor_ids for tile in owner.get(aid,[])},key=lambda s:s.encode())
        if not options:options = candidates(node) if isinstance(node, dict) else []
        if not options and obj.kind == "FIELD":
            pointer = obj.original_ref.pointer.split("/fields/")[0]
            options = candidates(index.doc.resolve(pointer))
        if not options and obj.kind == "FAMILY":
            # Earliest supporting member's provenance with full family scope retained in packet.
            options = sorted(
                {
                    t
                    for r in index.package.payload["results"]
                    if r["group_id"] == obj.object_id
                    for t in candidates(r)
                },
                key=lambda s: s.encode(),
            )
        object_bucket[obj.object_id] = options[0] if options else "UNANCHORED"
        # Administrative source IDs / extraction status add no asserted scientific information.
        if obj.kind == "SOURCE" or obj.object_id == "$extraction":
            continue
        owned[object_bucket[obj.object_id]].append(obj.original_ref)
    for relation in index.relations:
        choices = sorted(
            {
                object_bucket.get(relation.source_id, "UNANCHORED"),
                object_bucket.get(relation.target_id, "UNANCHORED"),
            }
        )
        key = next((v for v in choices if v != "UNANCHORED"), "UNANCHORED")
        owned[key].append(relation.original_ref)
    return [
        SourceBucket(
            bucket_id=k,
            output_alias=index.alias,
            kind="UNANCHORED" if k == "UNANCHORED" else "SOURCE_TILE",
            tile_id=None if k == "UNANCHORED" else k,
            assertion_refs=v,
        )
        for k, v in sorted(owned.items())
    ]


class PaperEvaluator:
    def __init__(self, ops):
        self.ops = ops

    def evaluate(
        self,
        *,
        source_packet,
        source_frame,
        packages,
        comparison_plan,
        secret,
        commitment,
        policy_sha256,
        document_bindings,
        revision=0,
        generation_authorization=None,
    ):
        """All arguments are trusted immutable artifacts; packages includes every scheduled role."""
        store = self.ops.store
        tolerant=getattr(self.ops,'failure_tolerance_policy',None) is not None
        from .failure_tolerance import stage_event, failure
        validate_frame(source_frame, source_packet)
        from .docling_frame import validate_docling_frame
        docling_evidence=validate_docling_frame(store,source_frame,source_packet)
        if any(d.file and d.file.media_type=='application/pdf' for d in source_packet.documents) and not docling_evidence:
            raise ValueError('PDF evaluation requires a bound independent Docling source frame')
        from .layout_validation import validate_layout
        for table in source_frame.tables:
            validate_layout(store,table,source_frame,source_packet)
        for document in source_packet.documents:
            if document.file is not None:
                store.read(document.file)
            if document.required and document.availability != "AVAILABLE":
                raise Hold("SOURCE", "Required supplied source bytes unavailable")
        if len({p.output_alias for p in packages}) != len(packages):
            raise ValueError("Duplicate output alias")
        aliases = {p.output_alias for p in packages}
        active = {comparison_plan.baseline_alias, *comparison_plan.challenger_aliases}
        if not active <= aliases or comparison_plan.reference_alias not in aliases:
            raise ValueError("Incomplete compared pool")
        for p in packages:
            if (p.paper_id, p.round_id, p.source_packet_sha256) != (
                source_packet.paper_id,
                source_packet.round_id,
                sha(canonical(source_packet)),
            ):
                raise ValueError("Package/source identity mismatch")
            for f in [p.execution_receipt, p.extraction_json, *p.table_csv_files]:
                if f is not None:
                    store.read(f)
        pool = sha(
            canonical(
                sorted(
                    (p.model_dump(mode="json") for p in packages),
                    key=lambda x: x["output_alias"],
                )
            )
        )
        scope = (
            PROTOCOL,
            source_packet.round_id,
            comparison_plan.stage,
            source_packet.paper_id,
        )
        from .seed_continuation import verify as verify_seed_commitment
        verify_seed_commitment(store,scope=scope,source_packet_sha256=sha(canonical(source_packet)),
            source_frame_sha256=sha(canonical(source_frame)),current_policy_sha256=policy_sha256,
            input_pool_sha256=pool,secret=secret,original_commitment=commitment)
        binding = EvaluationBinding(
            protocol_id=PROTOCOL,
            paper_id=source_packet.paper_id,
            round_id=source_packet.round_id,
            stage=comparison_plan.stage,
            source_packet_sha256=sha(canonical(source_packet)),
            source_frame_sha256=sha(canonical(source_frame)),
            input_pool_sha256=pool,
            policy_sha256=policy_sha256,
            revision=revision,
        )
        if docling_evidence:
            from .source_recovery import validate_frame_scope
            validate_frame_scope(store,source_frame,binding)
        clock_core={k:getattr(binding,k) for k in ('protocol_id','round_id','stage','paper_id','source_packet_sha256','input_pool_sha256','policy_sha256')}
        key="generation:"+sha(canonical(clock_core))
        inherited_corrections=[]
        if revision:
            if generation_authorization is None:raise ValueError("A scientific correction requires explicit common authorization")
            from .contracts import CorrectionRecord
            prior_ref=generation_authorization["predecessor"]
            prior=store.load(prior_ref,SampledSilverReceipt)
            correction=generation_authorization["correction"]
            if not isinstance(correction,CorrectionRecord) or correction.superseded_receipt_sha256!=prior_ref.sha256 or prior.binding.revision+1!=revision:
                raise ValueError("Scientific correction predecessor/revision mismatch")
            if any(getattr(prior.binding,k)!=v for k,v in clock_core.items()):raise ValueError("Correction changed original source/output/policy scope")
            inherited_corrections=[*prior.corrections,correction]
            if sum(c.kind in {"SOURCE_FRAME","SOURCE_TASK"} for c in inherited_corrections)>2:raise ValueError("Common correction limit exhausted")
            if prior.state=="FROZEN":key+=":authorized:"+sha(canonical(generation_authorization))
        _,deadline=store.generation(key,sha(canonical(clock_core)),timeout=self.ops.generation_timeout_seconds)
        self.ops.deadline=min(self.ops.deadline,deadline)
        source_ineligible=not any(t.eligible_content for t in source_frame.tiles) and not any(t.numerical for t in source_frame.tables)
        selected = samples(source_frame.tiles, secret, scope)
        tilemap = {t.tile_id: t for t in source_frame.tiles}
        work_items = []
        measured = {}
        all_cards = {}
        tasks = []
        packets = []
        verdicts = []
        gates = {}
        vectors = []
        held = []
        if getattr(self.ops,'budgeted_profile',None) is not None:
            held.append(ErrorRecord(category='EVALUATOR',code='BUDGETED_DIAGNOSTIC_ONLY',stage='VERIFICATION',
                message='Experimental indexed retrieval and batched judgments are not qualified guide validation',
                retryable=False,output_refs=[]))
        allbuckets = []
        selections = []
        if any(p.state=='INFRASTRUCTURE_FAILURE' for p in packages):
            # An unavailable original execution is a common uncertainty. Finish
            # the deterministic gate ledger without paying to judge competitors.
            held.extend(e for p in packages if p.state=='INFRASTRUCTURE_FAILURE' for e in p.errors)
        from threading import Lock
        tasks_lock=Lock();progress_lock=Lock()
        table_count_locks={table.table_uid:Lock() for table in source_frame.tables}
        controls=getattr(self.ops,"diagnostic_runtime_controls",None) or {}
        package_workers=controls.get("max_submissions_per_paper",1) if tolerant else 1
        progress_path=self.ops.trace_dir.parent/"output-progress.jsonl"
        def progress(event,alias=None,**extra):
            if package_workers==1:return
            import json,time
            with progress_lock,progress_path.open("a") as output:
                output.write(json.dumps({"time":time.time(),"binding_sha256":sha(canonical(binding)),"event":event,"output_alias":alias,**extra})+"\n")
        progress("PLAN",output_aliases=[p.output_alias for p in packages if p.output_alias in active])
        tasks_initialized=False
        def ensure_shared_tasks():
            with tasks_lock:ensure_shared_tasks_locked()
        def ensure_shared_tasks_locked():
            # G=0 needs no scientific judgment. The source-only task payload and
            # precommitted samples remain independent of submitted representations.
            nonlocal tasks_initialized
            if tasks_initialized:return
            tasks_initialized=True
            try:
                def build(tileid,local):
                    tile = tilemap[tileid]
                    evidence = evidence_for(
                        source_frame, tile.owned_anchor_ids + tile.context_anchor_ids
                    )
                    if getattr(local,'harness',None):
                        from .harness import same_page_context
                        evidence=same_page_context(source_frame,evidence)
                    try:
                        result=selected_cards(local,tile,binding,evidence,
                            expand=lambda current,requests,limit:expand_evidence(source_frame,current,requests,limit,tokenizers=local.tokenizers))
                    except Exception as exc:
                        if not tolerant:raise
                        record=failure(local,exc,'TASK_BUILD');held.append(record)
                        stage_event(self.ops,'SOURCE_TASK_BUILD',tileid,[record])
                        return []
                    if tolerant:stage_event(self.ops,'SOURCE_TASK_BUILD',tileid,card_count=len(result))
                    return result
                tileids=selected['content_prose']+selected['content_table']
                if getattr(self.ops,'harness',None) or getattr(self.ops,'parallel_diagnostic',False):
                    from concurrent.futures import ThreadPoolExecutor
                    children=[self.ops.scoped() for _ in tileids]
                    try:
                        with ThreadPoolExecutor(max_workers=worker_limit(self.ops)) as executor:
                            for cards in executor.map(lambda item:build(*item),zip(tileids,children)):tasks.extend(cards)
                    finally:
                        for local in children:
                            self.ops.used.extend(local.used);self.ops.run_refs.extend(local.run_refs)
                            self.ops.packet_refs.extend(local.packet_refs);self.ops.response_refs.update(local.response_refs)
                else:
                    for tileid in tileids:tasks.extend(build(tileid,self.ops))
                for tileid in selected["archive"]:
                    tile = tilemap[tileid]
                    tasks.append(
                        TaskCard(
                            task_id="archive-" + sha(canonical([binding, tileid])),
                            task_kind="ARCHIVE",
                            binding=binding,
                            tile_ids=[tileid],
                            output_alias=None,
                            source_bucket_id=None,
                            requirements=[
                                SourceRequirement(
                                    requirement_id="archive-complete",
                                    statement="Faithfully preserve every selected source row and all logical columns, exact display precision, caption, headers, panels, units, markers, annotations and scoped table links.",
                                    qualifications=[],
                                    evidence_anchor_ids=tile.owned_anchor_ids
                                    + tile.context_anchor_ids,
                                    encoding_constraints=[],
                                )
                            ],
                            evidence_anchor_ids=tile.owned_anchor_ids
                            + tile.context_anchor_ids,
                            source_scope_note="Selected complete five-row source block; do not require an F per CSV cell.",
                        )
                    )
            except SourceInventoryDefect as exc:
                self.ops.pending_source_correction=exc.proposal
                held.append(hold_record(exc,'TASK_BUILD'))
            except Hold as exc:
                held.append(hold_record(exc,'TASK_BUILD'))
        from concurrent.futures import ThreadPoolExecutor
        def check(item):
            task,alias,index,package_hash,db,occupied,table_uid_by_output=item
            local=self.ops.scoped();packet_refs=[];verdict_refs=[];bits=[];errors=[];children=[]
            try:
                owned_refs=next((b.assertion_refs for b in occupied if b.bucket_id==task.source_bucket_id),None)
                match=(index.audit_match(task,store,owned_refs) if task.task_kind=='OUTPUT_AUDIT'
                       else index.archive_match(task,store,{o for o,uid in table_uid_by_output.items() if uid==tilemap[task.tile_ids[0]].table_uid}) if task.task_kind=='ARCHIVE'
                       else index.match(task,local,evidence=evidence_for(source_frame,task.evidence_anchor_ids)))
                ranges={output:(tilemap[task.tile_ids[0]].first_data_row,tilemap[task.tile_ids[0]].stop_data_row)
                    for output,uid in table_uid_by_output.items() if task.task_kind=='ARCHIVE' and tilemap[task.tile_ids[0]].table_uid==uid}
                packet=index.packet(task,binding,match,package_hash,db,owned_refs=owned_refs,table_ranges=ranges)
                from .sharding import partition
                try:
                    if getattr(local,'harness',None) and task.task_kind=='OUTPUT_AUDIT':
                        shards=partition(index,task,packet,tokenizers=local.tokenizers,owned_refs=owned_refs,
                            table_ranges=ranges,source_frame=source_frame,force_audit_groups=True,unbounded_files=True)
                    elif getattr(local,'harness',None):shards=[(task,packet)]
                    else:shards=partition(index,task,packet,tokenizers=local.tokenizers,owned_refs=owned_refs,table_ranges=ranges,source_frame=source_frame)
                except Hold as exc:
                    if task.task_kind!='CONTENT' or task.scope_kind!='SCIENTIFIC_RESULT' or exc.category!='EVALUATOR':raise
                    from .match_identity import refine_identity
                    match=refine_identity(index,local,task,match,source_frame)
                    packet=index.packet(task,binding,match,package_hash,db)
                    shards=partition(index,task,packet,tokenizers=local.tokenizers,source_frame=source_frame)
                children.extend(child for child,_ in shards if child!=task)
                from .evidence_batching import prepare
                prepared=prepare(local,source_frame,shards)
                if getattr(local,'budgeted_profile',None) is not None:
                    from .budgeted_validation import assess_shards
                    assessed=assess_shards(local,prepared,secret=secret,scope=scope,resolve=index.resolve,
                        expand=lambda child_ops,current,requests,limit:expand_evidence(source_frame,current,requests,limit,tokenizers=child_ops.tokenizers))
                else:assessed=None
                for ordinal,(child,rendered,source_evidence,lookup) in enumerate(prepared):
                    packet_refs.append(store.put(rendered))
                    verdict=assessed[ordinal][2] if assessed is not None else assess(local,task=child,packet=rendered,evidence=source_evidence,source_lookup=lookup,
                        secret=secret,scope=scope,resolve=index.resolve,
                        expand=lambda current,requests,limit:expand_evidence(source_frame,current,requests,limit,tokenizers=local.tokenizers))
                    verdict_ref=store.put(verdict);verdict_refs.append(verdict_ref)
                    if verdict.state=='HOLD':errors.extend(verdict.hold_reasons)
                    else:bits.append(TaskBit(task_id=child.task_id,tile_or_bucket_id=child.source_bucket_id or child.tile_ids[0],
                        task_kind=child.task_kind,bit=int(verdict.state=='PASS'),verdict_sha256=verdict_ref.sha256))
            except Hold as exc:
                errors.append(hold_record(exc,'VERIFICATION'))
            except Exception as exc:
                if not tolerant:raise
                errors.append(failure(local,exc,'VERIFICATION'))
            return alias,packet_refs,verdict_refs,bits,errors,local,children,task
        by_alias=defaultdict(list)
        case_executor=None
        def consume(work_items):
            ordered=sorted(work_items,key=lambda x:(x[0].task_id,x[1]))
            if ordered:progress('CASES_PLANNED',ordered[0][1],count=len(ordered))
            if case_executor is not None:
                collect(case_executor.map(check,ordered))
            else:
                with ThreadPoolExecutor(max_workers=worker_limit(self.ops)) as executor:
                    collect(executor.map(check,ordered))
        def collect(results):
            for alias,packet_refs,verdict_refs,bits,errors,local,children,task in results:
                packets.extend(packet_refs+local.packet_refs);verdicts.extend(verdict_refs)
                by_alias[alias].extend(bits)
                if tolerant:
                    if not bits and not errors:errors.append(failure(local,ValueError('No scientific task bits produced'),'VERIFICATION'))
                    stage_event(self.ops,'SCIENTIFIC_CASE',task.task_id,errors,output_alias=alias,
                        task_kind=task.task_kind,unit_id=task.source_bucket_id or task.tile_ids[0],
                        bit=min((b.bit for b in bits),default=None))
                for child in children:all_cards[child.task_id]=child
                held.extend(errors);self.ops.run_refs.extend(local.run_refs);self.ops.used.extend(local.used)
                progress('CASE_FINISHED',alias,task_id=task.task_id,task_kind=task.task_kind,status='UNRESOLVED' if errors else 'COMPLETED')
        def evaluate_package(package,package_ops):
            if package.output_alias not in active:
                return
            alias = package.output_alias
            work_items=[]
            per_selected = {**selected, "output_audit": []}
            if package.state == "INFRASTRUCTURE_FAILURE":
                gates[alias] = GateReceipt(
                        output_alias=alias,
                        state="EVALUATOR_HOLD",
                        gate_bit=None,
                        errors=package.errors,
                    )
                if source_ineligible:vectors.append(score(binding,alias,None,source_frame,per_selected,[]))
                else:held.extend(package.errors)
                return
            if package.state == "PACKAGE_FAILURE":
                gate = GateReceipt(
                    output_alias=alias,
                    state="PIPELINE_FAILURE",
                    gate_bit=0,
                    errors=package.errors,
                )
                gates[alias] = gate
                vectors.append(score(binding, alias, 0, source_frame, per_selected, []))
                return
            with tempfile.TemporaryDirectory(prefix="claims-source-score-") as work:
                root = Path(work)
                (root / "output.json").write_bytes(store.read(package.extraction_json))
                for f in package.table_csv_files:
                    (root / Path(f.relative_path).name).write_bytes(store.read(f))
                # FileRef storage names are not delivered filenames: the package manifest retains exact name.
                original = Package(root, alias, paper_id=source_packet.paper_id)
                gate = original.gate
                gates[alias] = gate
                if source_ineligible:
                    vectors.append(score(binding,alias,gate.gate_bit,source_frame,per_selected,[]));return
                if gate.gate_bit and len(original.payload["tables"]) < len(
                    source_frame.tables
                ):
                    gate = GateReceipt(
                        output_alias=alias,
                        state="PIPELINE_FAILURE",
                        gate_bit=0,
                        errors=[
                            error(
                                "TABLE_PACKAGE_OR_COVERAGE",
                                "A required source table is missing from the delivered package",
                            )
                        ],
                    )
                    gates[alias] = gate
                if gate.gate_bit == 0:
                    vectors.append(
                        score(binding, alias, 0, source_frame, per_selected, [])
                    )
                    return
                if held and not tolerant:return  # Diagnostic continuation retains uncertainty without blocking unrelated work.
                index = Index(original)
                db = document_bindings.get(alias)
                if db is None:
                    from .document_binding import resolve_documents
                    try:db=resolve_documents(package_ops,index=index,source_packet=source_packet,source_frame=source_frame)
                    except Exception as exc:
                        if not tolerant and not isinstance(exc,Hold):raise
                        record=failure(package_ops,exc,'MECHANICAL');held.append(record)
                        if tolerant:stage_event(package_ops,'SOURCE_BINDING',alias,[record],output_alias=alias)
                        return
                if tolerant:stage_event(package_ops,'SOURCE_BINDING',alias,output_alias=alias)
                if any(b.state=='VERIFIED_WRONG' for b in db):
                    gate=GateReceipt(output_alias=alias,state='PIPELINE_FAILURE',gate_bit=0,
                        errors=[error('REFERENCE_UNRESOLVED','Submitted source/version contradicts the trusted execution inputs')])
                    gates[alias]=gate;vectors.append(score(binding,alias,0,source_frame,per_selected,[]));return
                defined_sources = {s["id"] for s in original.payload["sources"]}
                if {b.source_id for b in db} != defined_sources or any(
                    b.state != "BOUND" for b in db
                ):
                    held.append(
                        ErrorRecord(
                            category="SOURCE",
                            code="SOURCE_VERSION_UNRESOLVED",
                            stage="MECHANICAL",
                            message="Trusted worker source-version bindings are incomplete",
                            retryable=False,
                            output_refs=[],
                        )
                    )
                    return
                index.document_uid_by_source={b.source_id:b.document_uid for b in db if b.state=='BOUND'}
                # Exhaustive required-table/source delivery, independent of sampled T fidelity.
                supplied={d.document_uid for d in source_packet.documents if d.file is not None}
                reviewed={b.document_uid for b in db if b.source_id in original.payload["extraction"]["table_coverage"]["reviewed_source_ids"]}
                delivery_errors=[]
                if supplied-reviewed:delivery_errors.append(error("TABLE_PACKAGE_OR_COVERAGE","A supplied in-scope source was not reviewed"))
                mapped=[]
                table_uid_by_output={}
                table_mapping_unresolved=False
                for source_table in source_frame.tables:
                    table_anchors=list(dict.fromkeys(source_table.anchor_ids+source_table.header_anchor_ids+source_table.note_anchor_ids))
                    target=TaskCard(task_id="inventory-"+source_table.table_uid,task_kind="ARCHIVE",binding=binding,tile_ids=[t.tile_id for t in source_frame.tiles if t.table_uid==source_table.table_uid],
                        output_alias=None,source_bucket_id=None,requirements=[SourceRequirement(requirement_id="table-identity",
                        statement="Identify the one original submitted TableObject corresponding to this independent source table, combining its panels/continuations. This is correspondence only, not cell fidelity: "+canonical(source_table).decode(),
                        qualifications=[],evidence_anchor_ids=table_anchors,encoding_constraints=[])],
                        evidence_anchor_ids=table_anchors,source_scope_note="Exhaustive source-table inventory mapping; do not score cell contents or scientific result omissions.")
                    try:
                        from .table_matching import resolve_table
                        candidates=[resolve_table(index,package_ops,target,evidence_for(source_frame,table_anchors))]
                        if len(candidates[0])!=1:
                            delivery_errors.append(error("TABLE_PACKAGE_OR_COVERAGE","Required source table missing/duplicated: "+source_table.table_uid))
                        else:
                            output_table=next(iter(candidates[0]));mapped.append(output_table)
                            table_uid_by_output[output_table]=source_table.table_uid
                            declared=next(t for t in original.payload["tables"] if t["id"]==output_table)
                            from .packets import source_data_rows
                            physical_rows=source_data_rows(declared,index.package.csv[output_table],source_table.data_row_count,
                                source_header_texts=[a.exact_text for a in source_frame.anchors if a.anchor_id in source_table.header_anchor_ids])
                            if physical_rows is None:
                                from .table_count_review import verify_source_count,FAILURE_PREFIX
                                with table_count_locks[source_table.table_uid]:
                                    verify_source_count(package_ops,source_frame,source_table)
                                delivery_errors.append(error("CSV_FORMAT_OR_DIMENSIONS",FAILURE_PREFIX+source_table.table_uid))
                            else:index.table_data_rows[output_table]=physical_rows
                    except Exception as exc:
                        if not tolerant and not isinstance(exc,Hold):raise
                        record=failure(package_ops,exc,'MATCHING');held.append(record);table_mapping_unresolved=True
                        if tolerant:stage_event(package_ops,'TABLE_BINDING',alias+':'+source_table.table_uid,[record],output_alias=alias)
                    else:
                        if tolerant:stage_event(package_ops,'TABLE_BINDING',alias+':'+source_table.table_uid,output_alias=alias)
                if len(mapped)!=len(set(mapped)):
                    delivery_errors.append(error("TABLE_PACKAGE_OR_COVERAGE","Distinct required source tables mapped to one delivered table"))
                if delivery_errors:
                    gate=GateReceipt(output_alias=alias,state="PIPELINE_FAILURE",gate_bit=0,errors=delivery_errors)
                    gates[alias]=gate
                    vectors.append(score(binding,alias,0,source_frame,per_selected,[]));return
                index.table_uid_by_output=table_uid_by_output
                ensure_shared_tasks()
                if held and not tolerant:return  # Strict guide receipts remain HOLD; diagnostics continue independent cases.
                own_tasks=[t for t in tasks if not (tolerant and table_mapping_unresolved and t.task_kind=='ARCHIVE'
                    and tilemap[t.tile_ids[0]].table_uid not in set(table_uid_by_output.values()))]
                occupied = buckets(index, source_frame, db, table_uid_by_output)
                allbuckets.extend(occupied)
                chosen = audit_buckets([b.bucket_id for b in occupied], secret, scope)
                per_selected["output_audit"] = chosen
                if not (tolerant and table_mapping_unresolved):
                    selections.append(selection_manifest(store,source_frame.tiles,secret,scope,"output_audit",chosen,
                                      alias=alias,population=[b.bucket_id for b in occupied]))
                for bucket in occupied:
                    if tolerant and table_mapping_unresolved:continue  # Unknown ownership is not a scientific P=1 or a valid sample.
                    if bucket.bucket_id not in chosen:
                        continue
                    tile_ids = [] if bucket.tile_id is None else [bucket.tile_id]
                    anchor_ids = (
                        [a.anchor_id for a in source_frame.anchors]
                        if bucket.tile_id is None
                        else tilemap[bucket.tile_id].owned_anchor_ids
                        + tilemap[bucket.tile_id].context_anchor_ids
                    )
                    own_tasks.append(
                        TaskCard(
                            task_id="audit-" + sha(canonical([binding, alias, bucket])),
                            task_kind="OUTPUT_AUDIT",
                            binding=binding,
                            tile_ids=tile_ids,
                            output_alias=alias,
                            source_bucket_id=bucket.bucket_id,
                            requirements=[],
                            evidence_anchor_ids=anchor_ids,
                            source_scope_note="Verify EVERY assertion owned by packet.match.candidate_refs. Other rendered objects and relations provide necessary context, not additional audit ownership. The complete parent-bucket ownership ledger is retained in the sampling manifest; mandatory shards collectively cover it. Raw archival cells are assessed only in T.",
                        )
                    )
                measured[alias]=per_selected
                for task in own_tasks:
                    all_cards[task.task_id]=task
                    work_items.append((task,alias,index,sha(canonical(package)),db,occupied,table_uid_by_output))
                # This immutable index stays private to its submission worker.
                consume(work_items)
        def execute_package(package):
            if package.output_alias not in active:return
            alias=package.output_alias
            package_ops=self.ops.scoped() if package_workers>1 else self.ops
            progress('PACKAGE_STARTED',alias)
            try:
                evaluate_package(package,package_ops)
            except Exception as exc:
                if not tolerant:raise
                record=failure(package_ops,exc,'MECHANICAL');held.append(record)
                gates.setdefault(alias,GateReceipt(output_alias=alias,state='EVALUATOR_HOLD',gate_bit=None,errors=[record]))
                stage_event(self.ops,'PACKAGE_EVALUATION',alias,[record],output_alias=alias)
            finally:
                if package_ops is not self.ops:
                    self.ops.run_refs.extend(package_ops.run_refs);self.ops.used.extend(package_ops.used)
                    self.ops.packet_refs.extend(package_ops.packet_refs);self.ops.response_refs.update(package_ops.response_refs)
                    self.ops.continuation_events.extend(package_ops.continuation_events)
                progress('PACKAGE_FINISHED',alias,gate=gates[alias].gate_bit if alias in gates else None)
        if package_workers>1:
            with ThreadPoolExecutor(max_workers=worker_limit(self.ops)) as shared_cases:
                case_executor=shared_cases
                with ThreadPoolExecutor(max_workers=package_workers) as submissions:
                    list(submissions.map(execute_package,packages))
        else:
            for package in packages:execute_package(package)
        gates=[gates[p.output_alias] for p in packages if p.output_alias in active]
        for alias,per_selected in measured.items():
            vectors.append(score(binding,alias,1,source_frame,per_selected,by_alias[alias],hold=bool(held)))
        # Any common uncertainty nulls every score, including a gate-failed pipeline's provisional zero.
        if held:
            vectors = [
                score(
                    binding, p.output_alias, next((g.gate_bit for g in gates if g.output_alias==p.output_alias),None), source_frame, selected, [], hold=True
                )
                for p in packages
                if p.output_alias in active
            ]
        for name in ("content_prose","content_table","archive"):
            selections.append(selection_manifest(store,source_frame.tiles,secret,scope,name,selected[name]))
        if package_workers>1:
            # Completion order is operational, never part of the scientific
            # receipt identity. Cached parallel replay stays byte-identical.
            selections.sort(key=lambda s:(s.output_alias or '',s.stream))
            allbuckets.sort(key=lambda b:(b.output_alias,b.bucket_id))
            packets.sort(key=lambda r:r.sha256);verdicts.sort(key=lambda r:r.sha256)
            vectors.sort(key=lambda v:v.output_alias);held.sort(key=canonical)
        manifest = SampleManifest(
            binding=binding,
            seed_commitment=commitment,
            revealed_seed_hex=secret.hex(),
            selections=selections,
            output_buckets=allbuckets,
            content_available=any(t.eligible_content for t in source_frame.tiles),
            archive_available=any(t.numerical for t in source_frame.tables),
        )
        ineligible = not manifest.content_available and not manifest.archive_available
        recorded_cards={**{t.task_id:t for t in tasks}, **all_cards}
        ordered_cards=[recorded_cards[key] for key in sorted(recorded_cards)] if package_workers>1 else recorded_cards.values()
        recorded_runs=list(dict.fromkeys(self.ops.run_refs))
        if package_workers>1:recorded_runs.sort(key=lambda r:r.sha256)
        receipt = SampledSilverReceipt(
            binding=binding,
            state="HOLD" if held else "INELIGIBLE" if ineligible else "FROZEN",
            source_packet=store.put(source_packet),
            source_frame=store.put(source_frame),
            comparison_plan=store.put(comparison_plan),
            input_packages=[store.put(p) for p in packages],
            sampling_manifest=store.put(manifest),
            task_cards=[store.put(t) for t in ordered_cards],
            assertion_packets=packets,
            judge_runs=recorded_runs,
            deterministic_gates=gates,
            scientific_verdicts=verdicts,
            paper_scores=vectors,
            corrections=inherited_corrections,
            hold_reasons=held,
        )
        ref = store.put(receipt)
        if tolerant:
            from .failure_tolerance import project
            projection=project(receipt=receipt,frame=source_frame,manifest=manifest,events=self.ops.continuation_events,
                               tolerance=self.ops.failure_tolerance_policy)
            projection['strict_receipt']=ref.model_dump(mode='json')
            projected=store.put(projection)
            self.ops.failure_tolerance_projection=projected
            projection_head='failure-tolerance:'+ref.sha256
            previous=store.head(projection_head)
            if previous is None:store.advance(projection_head,None,projected.sha256)
        proposal=getattr(self.ops,'pending_source_correction',None)
        if proposal is not None:
            proposal_key='source-correction-proposal:'+sha(canonical(binding))
            old_proposal=store.head(proposal_key)
            if old_proposal is None:store.advance(proposal_key,None,proposal.sha256)
            elif old_proposal!=proposal.sha256:raise ValueError('Common source correction proposal changed')
        import json
        head_key="paper:"+json.dumps([source_packet.round_id,comparison_plan.stage,source_packet.paper_id])
        current=store.head(head_key)
        if current is not None and current!=ref.sha256:
            prior=SampledSilverReceipt.model_validate_json(store.get_blob(current),strict=True)
            if (prior.state==receipt.state=='HOLD' and prior.binding==binding
                and held and all(e.code=='DEADLINE_EXCEEDED' for e in held)
                and {r.sha256 for r in receipt.judge_runs}<={r.sha256 for r in prior.judge_runs}):
                # A poll after expiry must not replace useful original findings
                # and paid-work references with a generic zero-work timeout.
                raw=store.get_blob(current)
                from .contracts import FileRef
                return prior,FileRef(relative_path='blobs/'+current,sha256=current,byte_length=len(raw),media_type='application/octet-stream')
            if prior.state=="FROZEN" and generation_authorization is None:
                raise ValueError("A frozen scientific receipt cannot change without common correction authorization")
            if prior.binding!=binding and (generation_authorization is None or generation_authorization["predecessor"].sha256!=current):
                raise ValueError("Stale scientific generation predecessor")
        store.advance_paper_revision(source_packet.round_id,comparison_plan.stage,source_packet.paper_id,current,ref.sha256)
        return receipt, ref


def expand_evidence(source_frame,current,requests,limit,*,tokenizers=None):
    """Retrieve only captured evidence from the same immutable source frame; never URLs."""
    from .packets import tokens
    anchors={a.anchor_id:a for a in source_frame.anchors}
    selected={a['anchor_id'] for a in current['anchors']}
    before=set(selected)
    optional=[]
    for request in requests:
        if not set(request.anchor_ids)<=anchors.keys():raise Hold('EVALUATOR','Unknown requested source anchor')
        candidates=[a for a in source_frame.anchors if request.document_uid is None or a.document_uid==request.document_uid]
        if request.document_uid is not None and not candidates:raise Hold('SOURCE','Requested document was not supplied')
        selected.update(request.anchor_ids)
        # Docling provenance links table captions/notes to their headers. A request
        # for adjacent text may need those associated headers and OCR context too.
        for table in source_frame.tables:
            if set(request.anchor_ids)&set(table.anchor_ids+table.data_row_anchor_ids+table.header_anchor_ids+table.note_anchor_ids):
                selected.update(table.header_anchor_ids+table.note_anchor_ids)
        pages={anchors[aid].locator.page_index for aid in request.anchor_ids if anchors[aid].locator.kind=='page'}
        if request.kind in {'TABLE_HEADERS','TABLE_NOTES'}:
            for table in source_frame.tables:
                if set(request.anchor_ids)&set(table.anchor_ids+table.data_row_anchor_ids+table.header_anchor_ids+table.note_anchor_ids):
                    # Native links can miss a footnote that Docling captured as
                    # ordinary page text. Keep the links and retrieve that page.
                    context=table.header_anchor_ids+table.note_anchor_ids
                    selected.update(context)
                    pages.update(anchors[aid].locator.page_index for aid in context if anchors[aid].locator.kind=='page')
        elif request.kind=='ADJACENT_TEXT':
            indices=[i for i,a in enumerate(candidates) if a.anchor_id in request.anchor_ids]
            for i in indices:
                # Immediate neighbors may already be in the owned tile. Find
                # the nearest unseen original anchor on each side instead.
                for direction in (-1,1):
                    for j in range(i+direction,len(candidates) if direction==1 else -1,direction):
                        if candidates[j].anchor_id not in selected:
                            optional.append(candidates[j].anchor_id);break
        else:
            query=tokens(request.query)
            ranked=sorted((a for a in candidates if a.anchor_id not in selected and query&tokens(a.exact_text or '')),
                key=lambda a:(-len(query&tokens(a.exact_text or '')),a.anchor_id.encode()))
            optional.extend(a.anchor_id for a in ranked[:12])
        if request.kind in {'TABLE_HEADERS','TABLE_NOTES','ADJACENT_TEXT'} and pages:
            same_page=[a for a in candidates if a.anchor_id not in selected and a.locator.kind=='page' and a.locator.page_index in pages]
            # Prefer explicit note text and independent page OCR, then nearby
            # parsed paragraphs. Every addition remains an original full anchor.
            same_page.sort(key=lambda a:(not (a.exact_text or '').lstrip().startswith(('*','†','‡','§')),
                '-ocr-page' not in a.anchor_id,a.anchor_id.encode()))
            optional.extend(a.anchor_id for a in same_page)
    from .transport import judge_evidence
    if tokenizers is None:
        from .tokenization import BudgetTokenizer
        tokenizers={'budget':BudgetTokenizer()}
    def fits(ids):
        wire=canonical(judge_evidence(evidence_for(source_frame,ids))).decode()
        return max(counter(wire) for counter in tokenizers.values())<=limit
    # Never truncate an explicit witness or silently lose the original evidence.
    if not fits(selected):raise Hold('SOURCE','Required source evidence exceeds current evidence bound')
    for aid in dict.fromkeys(optional):
        if aid not in selected and fits(selected|{aid}):selected.add(aid)
    if selected==before:raise Hold('SOURCE','Requested expansion adds no captured evidence')
    return evidence_for(source_frame,[a.anchor_id for a in source_frame.anchors if a.anchor_id in selected])
