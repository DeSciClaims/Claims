"""Lossless original-property rendering and complete deterministic descriptor search."""

from __future__ import annotations
import math
from collections import Counter
from .contracts import (
    PropertyAssertion,
    ObjectAssertion,
    RelationAssertion,
    ClosureView,
    CsvBlock,
    JsonRef,
    MatchResponse,
    MatchResult,
    SearchCoverage,
    RequirementMatch,
    AssertionPacket,
)
from .store import canonical, sha
from ..graph import closure_sets
from .operations import Hold

CONTEXT_HEADER_PROPERTIES = frozenset({'id','field','field_group','label','units','measure','context','origin','parent_field_id','field_path','title','name','question','investigation','group_id'})


def context_property_names(obj,refs):
    names=set(CONTEXT_HEADER_PROPERTIES)
    prefix=obj.original_ref.pointer+'/'
    for ref in refs:
        if ref.kind=='json' and ref.pointer.startswith(prefix):
            key=ref.pointer[len(prefix):].split('/',1)[0].replace('~1','/').replace('~0','~')
            if key!='fields':names.add(key)
    return names


FIELD_PROPERTIES = (
    "field",
    "field_group",
    "label",
    "value",
    "units",
    "value_operator",
    "measure",
    "confidence_level",
    "description",
    "context",
    "origin",
    "value_status",
    "notes",
    "parent_field_id",
    "field_path",
    "references",
    "source_snippets",
    "table_references",
)


def tokens(text):
    # Maximal Unicode letter/number runs, including non-ASCII scientific vocabulary.
    result = []
    part = []
    import unicodedata
    for char in unicodedata.normalize('NFKC',text).casefold():
        if char.isalpha() or char.isnumeric():
            part.append(char)
        elif part:
            result.append("".join(part))
            part = []
    if part:
        result.append("".join(part))
    return set(result)


class Index:
    def __init__(self, package):
        self.package = package
        self.alias = package.alias
        self.table_data_rows = {}
        self.doc = package.document
        self.objects = []
        self.relations = []
        self.closures = []
        self.originals = {}

        def ref(pointer):
            return JsonRef(kind="json", output_alias=self.alias, pointer=pointer)

        def props(pointer, node, expected=()):
            names = list(node) + [k for k in expected if k not in node]
            return [
                PropertyAssertion(
                    name=k,
                    present=k in node,
                    value_json=self.doc.literal(
                        pointer + "/" + k.replace("~", "~0").replace("/", "~1")
                    )
                    if k in node
                    else None,
                    original_ref=ref(
                        pointer + "/" + k.replace("~", "~0").replace("/", "~1")
                    )
                    if k in node
                    else None,
                )
                for k in names
                if k not in {"fields"}
            ]

        defined=[]
        for section in ('results','shared_context','cross_group_summaries','result_groups','tables','sources'):
            for i,record in enumerate(package.payload.get(section,[])):
                pointer='/'+section+'/'+str(i);defined.append((pointer,record))
                if section in {'results','shared_context','cross_group_summaries'}:
                    defined.extend((pointer+'/fields/'+str(j),field) for j,field in enumerate(record.get('fields',[])))
        defined.extend(('/data_code_availability/artifacts/'+str(i),artifact)
            for i,artifact in enumerate(package.payload.get('data_code_availability',{}).get('artifacts',[])))
        for pointer,node in sorted(defined,key=lambda item:item[0].encode()):
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                continue
            kind = (
                "FIELD"
                if "value" in node and "/fields/" in pointer
                else {
                    "results": "RESULT",
                    "shared_context": "SHARED_CONTEXT",
                    "cross_group_summaries": "CROSS_GROUP_SUMMARY",
                    "result_groups": "FAMILY",
                    "tables": "TABLE",
                    "sources": "SOURCE",
                    "data_code_availability": "SHARED_CONTEXT",
                }.get(pointer.split("/")[1] if "/" in pointer else "")
            )
            if kind is None:
                continue
            self.objects.append(
                ObjectAssertion(
                    object_id=node["id"],
                    kind=kind,
                    original_ref=ref(pointer),
                    properties=props(
                        pointer, node, FIELD_PROPERTIES if kind == "FIELD" else ()
                    ),
                )
            )
            self.originals[node["id"]] = (pointer, node)
        # Root scientific sidecars retain original pointers and cannot supply missing field values.
        for key in ("notes", "closure_review", "extraction"):
            if key in package.payload:
                self.objects.append(
                    ObjectAssertion(
                        object_id="$" + key,
                        kind="SHARED_CONTEXT",
                        original_ref=ref("/" + key),
                        properties=[
                            PropertyAssertion(
                                name=key,
                                present=True,
                                value_json=self.doc.literal("/" + key),
                                original_ref=ref("/" + key),
                            )
                        ],
                    )
                )
        if 'notes' in package.payload.get('extraction',{}):
            pointer='/extraction/notes'
            self.objects.append(ObjectAssertion(object_id='$extraction_notes',kind='SHARED_CONTEXT',original_ref=ref(pointer),
                properties=[PropertyAssertion(name='notes',present=True,value_json=self.doc.literal(pointer),original_ref=ref(pointer))]))
        availability=package.payload.get("data_code_availability",{})
        for category in ("data","code"):
            if category not in availability:continue
            pointer="/data_code_availability/"+category
            self.objects.append(ObjectAssertion(object_id="$availability_"+category,kind="SHARED_CONTEXT",original_ref=ref(pointer),
                properties=props(pointer,availability[category])))
        if "notes" in availability:
            pointer="/data_code_availability/notes"
            self.objects.append(ObjectAssertion(object_id="$availability_notes",kind="SHARED_CONTEXT",original_ref=ref(pointer),
                properties=[PropertyAssertion(name="notes",present=True,value_json=self.doc.literal(pointer),original_ref=ref(pointer))]))
        for i, edge in enumerate(package.payload["relationships"]):
            p = "/relationships/" + str(i)
            self.relations.append(
                RelationAssertion(
                    original_ref=ref(p),
                    source_id=edge["source_id"],
                    target_id=edge["target_id"],
                    relation=edge["relation"],
                    closure_direction=edge.get("closure_direction", "UNREVIEWED"),
                    properties=props(p, edge),
                )
            )
        for i, result in enumerate(package.payload["results"]):
            members, boundary = closure_sets(package.payload, result,package.graph)
            self.closures.append(
                ClosureView(
                    result_id=result["id"],
                    original_ref=ref("/results/" + str(i) + "/closure"),
                    submitted_core_ids=result["closure"]["core_ids"],
                    submitted_member_ids=result["closure"]["member_ids"],
                    submitted_reference_ids=result["closure"]["reference_ids"],
                    recomputed_member_ids=sorted(members),
                    recomputed_reference_ids=sorted(boundary),
                    unresolved_edge_refs=[ref('/relationships/'+str(k)) for k in sorted({i for key in members|{result['id']} for i in package.graph.unresolved[key]})],
                )
            )
        self.components = {
            o.original_ref.pointer: o.model_dump(mode="json") for o in self.objects
        }
        self.components.update(
            {r.original_ref.pointer: r.model_dump(mode="json") for r in self.relations}
        )
        self.components.update({c.original_ref.pointer:c.model_dump(mode='json') for c in self.closures})
        self.pointer_by_id={o.object_id:o.original_ref.pointer for o in self.objects}
        self.quote_index={}
        self.cell_index={}
        for key,(pointer,node) in self.originals.items():
            for snippet in node.get('source_snippets',[]):
                self.quote_index.setdefault(snippet['source_id'],[]).append((snippet['text'],pointer))
            for cell in node.get('table_references',[]):
                self.cell_index.setdefault((cell['table_id'],int(cell['row'])),set()).add(pointer)
        self.document_uid_by_source={}
        self.table_uid_by_output={}
        self.descriptors = {
            key: tokens(canonical(value).decode())
            for key, value in self.components.items()
        }
        self.df = Counter(t for value in self.descriptors.values() for t in value)
        self.inverted = {}
        for key, terms in self.descriptors.items():
            for term in terms:
                self.inverted.setdefault(term, set()).add(key)
        self.sha256 = sha(canonical(self.components))

    def ranked(self, query):
        query = tokens(query)
        N = len(self.descriptors)
        weights = {t: 1 + math.log((N + 1) / (self.df.get(t, 0) + 1)) for t in query}
        scores = {key: 0.0 for key in self.components}
        for term in sorted(query, key=lambda x: x.encode()):
            for key in self.inverted.get(term, ()):
                scores[key] += weights[term]
        return sorted(self.components, key=lambda key: (-scores[key], key.encode()))

    def source_hits(self,evidence):
        import unicodedata
        normalized=lambda text:' '.join(unicodedata.normalize('NFKC',text).casefold().split())
        result=set()
        for anchor in (evidence or {}).get('anchors',[]):
            text=normalized(anchor.get('exact_text') or '')
            for source,quotes in self.quote_index.items():
                if self.document_uid_by_source.get(source)!=anchor.get('document_uid'):continue
                for quote,pointer in quotes:
                    quote=normalized(quote)
                    if text and quote and (quote in text or text in quote):result.add(pointer)
            locator=anchor.get('locator',{})
            if locator.get('kind')=='table':
                for output,source in self.table_uid_by_output.items():
                    if source!=locator['table_uid']:continue
                    result.add(self.pointer_by_id[output])
                    physical=self.table_data_rows.get(output)
                    rows=physical[locator['first_row']:locator['stop_row']] if physical is not None else range(locator['first_row']+1,locator['stop_row']+1)
                    for row in rows:result.update(self.cell_index.get((output,row),()))
        # One explicit graph-neighbor step; context does not invent closure edges.
        ids={key for key,pointer in self.pointer_by_id.items() if pointer in result}
        for key in ids:
            for neighbor in self.package.graph.forward.get(key,set())|self.package.graph.reverse.get(key,set()):
                if neighbor in self.pointer_by_id:result.add(self.pointer_by_id[neighbor])
            for reference in self.originals.get(key,('',{}))[1].get('references',[]):
                if reference['target_id'] in self.pointer_by_id:result.add(self.pointer_by_id[reference['target_id']])
        return sorted(result,key=lambda p:p.encode())

    def resolve(self, ref):
        if ref.output_alias != self.alias:
            raise ValueError("Cross-pipeline pointer")
        if isinstance(ref, JsonRef):
            return self.doc.resolve(ref.pointer)
        table = next(
            (
                t
                for t in self.package.payload["tables"]
                if t["csv_file"] == ref.filename
            ),
            None,
        )
        if table is None:
            raise ValueError("Unknown delivered CSV")
        rows = self.package.csv[table["id"]]
        if not 0 <= ref.row < len(rows) or not 0 <= ref.column < len(rows[ref.row]):
            raise ValueError("CSV reference out of bounds")
        return rows[ref.row][ref.column]

    def match_context(self, keys):
        """Original identity/scope headers for relation endpoints and physical owners.

        Headers are explicitly partial retrieval context. The complete original
        blocks remain in the corpus and are still exhaustively scanned.
        """
        wanted=set()
        def owner(pointer):
            if '/fields/' in pointer:pointer=pointer.split('/fields/',1)[0]
            elif pointer.endswith('/closure'):pointer=pointer.rsplit('/closure',1)[0]
            else:return
            node=self.doc.resolve(pointer)
            if isinstance(node,dict) and isinstance(node.get('id'),str):wanted.add(node['id'])
        for key in keys:
            node=self.doc.resolve(key)
            owner(key)
            if isinstance(node,dict):
                for name in ('id','source_id','target_id','parent_field_id','group_id'):
                    if isinstance(node.get(name),str):wanted.add(node[name])
        for _ in range(2):
            for key in list(wanted):
                if key not in self.originals:continue
                pointer,node=self.originals[key]
                owner(pointer)
                for name in ('parent_field_id','group_id'):
                    if isinstance(node.get(name),str):wanted.add(node[name])
        names=set(FIELD_PROPERTIES)|{'id','title','name','question','investigation','group_id'}
        headers={}
        for key in sorted(wanted):
            if key not in self.originals:continue
            pointer,node=self.originals[key]
            values=[]
            for name in node:
                if name not in names:continue
                field_pointer=pointer+'/'+name.replace('~','~0').replace('/','~1')
                values.append({'name':name,'present':True,'value_json':self.doc.literal(field_pointer),
                    'original_ref':{'kind':'json','output_alias':self.alias,'pointer':field_pointer}})
            headers[key]={'object_id':key,'original_ref':{'kind':'json','output_alias':self.alias,'pointer':pointer},
                          'properties':values}
        return headers

    def match(self, task, ops, *, evidence=None, role="PRIMARY"):
        available = sorted(self.components, key=lambda s: s.encode())
        original_properties={p.original_ref.pointer for o in [*self.objects,*self.relations] for p in o.properties if p.present}
        inspected = []
        matches = {r.requirement_id: [] for r in task.requirements}
        candidates = []
        ambiguous = False
        ranks = self.ranked(canonical(task).decode()+' '+canonical(evidence or {}).decode())
        exact=self.source_hits(evidence)
        initial=exact+[key for key in ranks if key not in set(exact)][:12]
        remaining = [k for k in available if k not in initial]
        batches = [initial[i:i+32] for i in range(0,len(initial),32)] + [
            remaining[i : i + 32] for i in range(0, len(remaining), 32)
        ]
        def scan(item):
            keys,local=item

            def validate(response):
                if (
                    response.task_id != task.task_id
                    or response.output_alias != self.alias
                ):
                    raise ValueError("Wrong matching identity")
                if len(response.inspected_component_ids) != len(
                    set(response.inspected_component_ids)
                ) or set(response.inspected_component_ids) != set(keys):
                    raise ValueError("Incomplete search inspection: inspected_component_ids must contain exactly " + repr(keys))
                requested=[r.requirement_id for r in response.matches]
                if len(requested)!=len(set(requested)) or set(requested)!=set(matches):
                    raise ValueError('Every source requirement needs an explicit per-batch correspondence or empty result')
                for item in response.matches:
                    if item.requirement_id not in matches:
                        raise ValueError("Unknown requirement")
                    for ref in item.output_refs:
                        self.resolve(ref)
                        if task.scope_kind=='SOURCE_CONTEXT' and ref.kind=='json' and ref.pointer in self.components and ref.pointer not in original_properties:
                            raise ValueError('Context correspondence must cite original properties, not complete object/closure roots')
                assigned=[ref for item in response.matches for ref in item.output_refs]
                if response.state=='ABSENT' and assigned:raise ValueError('ABSENT correspondence cannot have matched references')
                if response.state in {'MATCHED','AMBIGUOUS'} and not assigned:raise ValueError('Matched correspondence needs requirement references')
                if response.state == "LOOKUP_UNRESOLVED":
                    raise Hold("EVALUATOR", "Candidate lookup unresolved")

            response = local.request(
                role,
                "source_match",
                {
                    "task": task.model_dump(mode="json"),
                    "output_alias": self.alias,
                    "scan_scope": "THIS_BATCH_ONLY",
                    "context_headers_are_partial": True,
                    "context_headers": self.match_context(keys),
                    "inspection_component_ids": keys,
                    "required_requirement_ids": list(matches),
                    "components": {k: self.components[k] for k in keys},
                    "authoritative_evidence": evidence or {},
                },
                MatchResponse,
                validate=validate,
            )
            return response
        # Batches are independent THIS_BATCH_ONLY lookups. Keep deterministic
        # batch-order aggregation, four request contexts per case, and the shared
        # Operations gate (16 globally). Every original block is still inspected.
        parallel=hasattr(ops,'scoped')
        children=[ops.scoped() for _ in batches] if parallel else [ops]*len(batches)
        try:
            if parallel:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=4) as executor:
                    responses=list(executor.map(scan,zip(batches,children)))
            else:
                responses=[scan(item) for item in zip(batches,children)]
        finally:
            if parallel:
                # Retain successful and failed attempt ledgers even if one batch
                # cannot finish. A failed scan never certifies global absence.
                for local in children:
                    ops.used.extend(local.used)
                    ops.run_refs.extend(local.run_refs)
                    ops.packet_refs.extend(local.packet_refs)
                    ops.response_refs.update(local.response_refs)
        for keys,response in zip(batches,responses):
            inspected.extend(keys)
            ambiguous |= response.state == "AMBIGUOUS"
            for m in response.matches:
                matches[m.requirement_id].extend(m.output_refs)
            candidates.extend(ref for item in response.matches for ref in item.output_refs)
        unique = lambda refs: list({canonical(r): r for r in refs}.values())
        candidates = unique(candidates)
        coverage = SearchCoverage(
            index_sha256=self.sha256,
            available_component_ids=ops.store.put(available),
            inspected_component_ids=ops.store.put(
                sorted(inspected, key=lambda s: s.encode())
            ),
            available_components=len(available),
            inspected_components=len(inspected),
            exhaustive_fallback_completed=True,
        )
        return MatchResult(
            task_id=task.task_id,
            output_alias=self.alias,
            state="AMBIGUOUS"
            if ambiguous and candidates
            else "MATCHED"
            if candidates
            else "ABSENT",
            matches=[
                RequirementMatch(requirement_id=k, output_refs=unique(v))
                for k, v in matches.items()
            ],
            candidate_refs=candidates,
            coverage=coverage,
            explanation="Complete original descriptor scan; correspondence is not scientific acceptance.",
        )

    def audit_match(self, task, store, owned_refs):
        """P audits known original assertions; it does not search for omitted results."""
        if task.task_kind != "OUTPUT_AUDIT" or task.output_alias != self.alias:
            raise ValueError("Known-assertion selection requires this output's audit task")
        refs = list({canonical(ref): ref for ref in owned_refs}.values())
        for ref in refs:
            self.resolve(ref)
        available = sorted(self.components, key=lambda key: key.encode())
        manifest = store.put(available)
        return MatchResult(
            task_id=task.task_id, output_alias=self.alias,
            state="MATCHED" if refs else "ABSENT", matches=[], candidate_refs=refs,
            coverage=SearchCoverage(index_sha256=self.sha256,
                available_component_ids=manifest, inspected_component_ids=manifest,
                available_components=len(available), inspected_components=len(available),
                exhaustive_fallback_completed=True),
            explanation="Controller enumerated the complete lossless index and selected the committed bucket ownership references. No scientific-omission or source-correspondence claim is made; the judges must verify every owned assertion.",
        )

    def archive_match(self, task, store, table_ids):
        """T owns the source-mapped archive; this is not a scientific-absence claim."""
        refs=[o.original_ref for o in self.objects if o.kind=='TABLE' and o.object_id in table_ids]
        available=sorted(self.components,key=lambda s:s.encode());manifest=store.put(available)
        return MatchResult(task_id=task.task_id,output_alias=self.alias,state='MATCHED' if refs else 'ABSENT',
            matches=[],candidate_refs=refs,coverage=SearchCoverage(index_sha256=self.sha256,
                available_component_ids=manifest,inspected_component_ids=manifest,
                available_components=len(available),inspected_components=len(available),exhaustive_fallback_completed=True),
            explanation='Controller enumerated the complete original index and selected the independently source-mapped archive. No scientific-content omission claim is made.')

    def packet(self, task, binding, match, package_hash, document_bindings, *, owned_refs=None, table_ranges=None):
        """Pure scoped rendering: retain all hits, contradictions and required graph/context.

        Boundary definitions do not recursively import their other members. P owns
        explicit original refs, independently of whatever correspondence selected.
        """
        refs = list(match.candidate_refs) + [r for m in match.matches for r in m.output_refs]
        if task.task_kind == "OUTPUT_AUDIT":
            if owned_refs is None:
                raise ValueError("Audit rendering requires the complete ownership ledger")
            refs.extend(owned_refs)
        byid = {o.object_id:o for o in self.objects}
        def contains(pointer, child):
            return pointer == child or child.startswith(pointer + "/")
        context_mode=task.task_kind=='CONTENT' and task.scope_kind=='SOURCE_CONTEXT'
        selected = set()
        matched_edges=set()
        for ref in refs:
            if isinstance(ref, JsonRef):
                selected.update(o.object_id for o in self.objects
                    if contains(o.original_ref.pointer, ref.pointer) or (task.task_kind!='OUTPUT_AUDIT' and not (context_mode and ref.pointer.startswith('/results/')) and contains(ref.pointer, o.original_ref.pointer)))
                for relation in self.relations:
                    if contains(ref.pointer,relation.original_ref.pointer) or contains(relation.original_ref.pointer,ref.pointer):
                        matched_edges.add(canonical(relation.original_ref))
                        if not context_mode:selected.update(endpoint for endpoint in (relation.source_id,relation.target_id) if endpoint in byid)
            else:
                selected.update(t['id'] for t in self.package.payload['tables'] if t['csv_file']==ref.filename)
        if task.task_kind=='ARCHIVE':
            # Include direct links into the selected original CSV rows as context,
            # without importing unrelated fields in their scientific closures.
            row_scope={}
            for table in self.package.payload['tables']:
                if table['id'] not in selected:continue
                ranges=(table_ranges or {}).get(table['id'])
                physical=self.table_data_rows.get(table['id'])
                row_scope[table['id']]=(set(physical[ranges[0]:ranges[1]] if physical is not None else range(ranges[0]+1,ranges[1]+1))
                    if ranges is not None else set(range(1,len(self.package.csv[table['id']]))))
            for oid,(pointer,node) in self.originals.items():
                if any(r['table_id'] in row_scope and int(r['row']) in row_scope[r['table_id']] for r in node.get('table_references',[])):
                    selected.add(oid)
        # Select local closures from original candidate owners once. Shared context
        # does not recursively activate every other result which uses that context.
        result_owners={o.original_ref.pointer:o.object_id for o in self.objects if o.kind=='RESULT'}
        active_results=set()
        for ref in refs:
            if ref.kind!='json':continue
            bits=ref.pointer.strip('/').split('/')
            if len(bits)>=2 and bits[0]=='results':
                owner='/'+ '/'.join(bits[:2])
                core=(len(bits)==2 or bits[2]=='closure' or
                    (bits[2]=='fields' and (len(bits)<=4 or bits[4] in {'value','value_operator','parent_field_id','field_path'})))
                if core and owner in result_owners:active_results.add(result_owners[owner])
            for relation in self.relations:
                if (ref.pointer==relation.original_ref.pointer or any(ref.pointer.startswith(relation.original_ref.pointer+'/'+name) for name in ('source_id','target_id','relation','closure_direction'))) and relation.relation not in {'applies_under','table_for'}:
                    for endpoint in (relation.source_id,relation.target_id):
                        if endpoint not in byid:continue
                        pointer=byid[endpoint].original_ref.pointer
                        owner='/'+ '/'.join(pointer.strip('/').split('/')[:2])
                        if owner in result_owners:active_results.add(result_owners[owner])
        descriptor_results={oid for oid in selected if byid[oid].kind=='RESULT'}-active_results
        if context_mode:active_results=set()
        active_closures=[c for c in self.closures if c.result_id in active_results]
        joint=set(selected)
        if task.task_kind!='ARCHIVE':
            for closure in active_closures:
                owner=byid[closure.result_id]
                fields={o.object_id for o in self.objects if o.kind=='FIELD' and contains(owner.original_ref.pointer,o.original_ref.pointer)}
                joint.update({closure.result_id,*fields,*closure.recomputed_member_ids})
        else:
            # T checks selected archival cells and their direct submitted links;
            # it does not import the scientific closures of all table consumers.
            active_closures=[]
        relevant=[];boundary=set()
        focus=joint-descriptor_results
        selected_fields={oid for oid in selected if byid[oid].kind=='FIELD'}
        local_scientific={oid for oid in focus if oid in self.originals and self.originals[oid][0].startswith(('/results/','/cross_group_summaries/'))}
        for relation in self.relations:
            context_scope=joint if task.task_kind=='ARCHIVE' else local_scientific
            context_link=relation.relation in {'table_for','applies_under'} and (relation.source_id in context_scope or relation.target_id in context_scope)
            explicit=canonical(relation.original_ref) in matched_edges
            if context_mode:
                include=(explicit or (relation.relation=='applies_under' and (relation.source_id in joint or relation.target_id in joint)) or (relation.relation=='derived_from' and (relation.source_id in selected_fields or relation.target_id in selected_fields)))
            elif relation.relation in {'applies_under','table_for'}:
                include=explicit or context_link
            else:
                include=relation.source_id in focus or relation.target_id in focus
            if include:
                relevant.append(relation)
                if not context_mode or relation.relation in {'applies_under','derived_from'}:boundary.update({relation.source_id,relation.target_id}-joint)
        for closure in active_closures:
            boundary.update(closure.recomputed_reference_ids)
        for oid in list(joint):
            original=self.originals.get(oid)
            if original is None:continue
            pointer,node=original
            # A physical owner supplies context, not an inferred closure edge.
            if '/fields/' in pointer:
                owner_pointer=pointer.split('/fields/')[0]
                joint.update(o.object_id for o in self.objects if o.original_ref.pointer==owner_pointer)
            if node.get('parent_field_id'):boundary.add(node['parent_field_id'])
            for ref in node.get('references',[]):
                if isinstance(ref,dict):boundary.update(ref[k] for k in ('target_id','object_id') if k in ref)
            boundary.update(r['table_id'] for r in node.get('table_references',[]))
        # A directly referenced shared definition includes its original fields as
        # boundary context, without traversing them into every consumer's closure.
        for oid in list(boundary):
            if oid in byid and byid[oid].kind=='SHARED_CONTEXT' and oid in self.originals:
                pointer=byid[oid].original_ref.pointer+'/fields/'
                boundary.update(o.object_id for o in self.objects if o.original_ref.pointer.startswith(pointer))
        # Full family declarations plus co-member identity/context preserve grouping witnesses.
        families={self.originals[oid][1].get('group_id') for oid in joint|boundary if oid in self.originals}
        families.update(oid for oid in joint if oid in byid and byid[oid].kind=='FAMILY')
        family_context=set()
        for obj in self.objects:
            if obj.kind=='FAMILY' and obj.object_id in families:boundary.add(obj.object_id)
            if not context_mode and obj.kind=='RESULT' and self.originals[obj.object_id][1].get('group_id') in families:family_context.add(obj.object_id)
        # Scientific root sidecars are checked for contradictions in every selected context.
        selected=joint|boundary|{o.object_id for o in self.objects if o.object_id.startswith('$')}
        objects=[o for o in self.objects if o.object_id in selected]
        # Context-only association properties need original endpoint identity/scope,
        # not the unrelated quantitative results in all endpoint owners. Headers
        # are explicitly partial and cannot supply an unrendered numeric value.
        header_ids={endpoint for edge in relevant for endpoint in (edge.source_id,edge.target_id)}-selected if context_mode else set()
        header_ids.update(family_context-selected)
        if context_mode:
            for obj in objects:
                prefix=obj.original_ref.pointer+'/'
                own=[r for r in refs if r.kind=='json' and (r.pointer==obj.original_ref.pointer or (r.pointer.startswith(prefix) and not r.pointer[len(prefix):].startswith('fields/')))]
                full=any(r.pointer==obj.original_ref.pointer or r.pointer==prefix+'value' or r.pointer.startswith(prefix+'value/') for r in own)
                if not full and (own or obj.kind=='RESULT'):header_ids.add(obj.object_id)
            objects=[o for o in objects if o.object_id not in header_ids]
        context_headers=[o.model_copy(update={'properties':[p for p in o.properties if p.name in
                (context_property_names(o,refs) if context_mode else CONTEXT_HEADER_PROPERTIES)]})
            for o in self.objects if o.object_id in header_ids]
        closures=active_closures
        table_ids={o.object_id for o in objects if o.kind=='TABLE'}
        csv_blocks=[]
        for t in self.package.payload['tables']:
            if t['id'] not in table_ids:continue
            rows=self.package.csv[t['id']]
            requested=set()
            if task.task_kind=='ARCHIVE' and table_ranges and t['id'] in table_ranges:
                first,stop=table_ranges[t['id']]
                physical=self.table_data_rows.get(t['id'])
                requested.update(physical[first:stop] if physical is not None else range(first+1,stop+1))
                if physical is not None:
                    # Section/panel-only CSV rows remain available as archival context.
                    requested.update(set(range(1,len(rows)))-set(physical))
            elif task.task_kind=='ARCHIVE':
                requested.update(range(1,len(rows)))
            for oid in joint:
                node=self.originals.get(oid,('',{}))[1]
                requested.update(int(r['row']) for r in node.get('table_references',[]) if r['table_id']==t['id'])
            # Separate complete single-row blocks keep exact CSV record coordinates.
            for row in sorted(requested):
                csv_blocks.append(CsvBlock(table_id=t['id'],filename=t['csv_file'],
                    file_sha256=sha(self.package.files[t['csv_file']]),first_row=row,
                    rows=[rows[row]],header=rows[0]))
        data=dict(task_id=task.task_id,binding=binding,output_alias=self.alias,
            submitted_package_sha256=package_hash,task_card_sha256=sha(canonical(task)),match=match,
            objects=objects,context_headers=context_headers,relations=relevant,closures=closures,csv_blocks=csv_blocks,
            document_bindings=document_bindings,evidence_anchor_ids=task.evidence_anchor_ids,
            source_bucket_ids=[task.source_bucket_id] if task.source_bucket_id else [],
            packet_complete=True,omitted_due_to_limit_refs=[])
        return AssertionPacket(packet_id='packet-'+sha(canonical(data)),**data)


def source_data_rows(table, csv_rows, expected_count):
    """Map scientific source-row ordinals to original CSV record coordinates.

    Archive row_count still counts every delivered CSV record. Extra records that
    contain only labels/section headings are context, not new numerical data rows.
    Original bytes and metadata are never rewritten.
    """
    all_rows=list(range(1,len(csv_rows)))
    if len(all_rows)==expected_count:return all_rows
    columns=[i for i,c in enumerate(table['columns']) if c['role']=='data']
    if not columns:return None
    data=[row for row in all_rows if any(csv_rows[row][column].strip() for column in columns)]
    return data if len(data)==expected_count else None
