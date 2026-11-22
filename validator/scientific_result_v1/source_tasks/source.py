"""Source-only inventory, independent review and selected-card preparation."""

from __future__ import annotations
from .budgets import SOURCE_TOKEN_BUDGETS
from .contracts import (
    InventoryResponse,
    SourceReviewResponse,
    SourceTaskBatch,
    TaskCard,
    SourceTableLayoutResponse,
    SourceTableLayoutRequest,
    SourcePartIdentity,
    SourceTable,
    SourceAnchor,
    TableLocator,
    SourceTableLayoutReceipt,
    SourceLogicalColumn,
    SourceColumnMember,
    SourcePartIdentity,
)
from .store import canonical, sha
from .operations import Hold, LayoutSubdivisionRequired


def exact_ids(actual, expected):
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("Incomplete/duplicate inspected IDs; expected exactly " + repr(expected))


def reviewed(ops, kind, proposal, request, anchors, items, *, source_limit=SOURCE_TOKEN_BUDGETS[0], allow_evidence_requests=False, proposal_validator=None):
    """Independent source review; substantive corrections only through source arbiter."""
    proposal_hash = sha(canonical(proposal))
    data = {
        **request,
        "proposal": proposal.model_dump(mode="json"),
        "proposal_hash": proposal_hash,
        "proposal_kind": kind,
        "checked_anchor_ids": anchors,
        "checked_item_ids": items,
        "response_echo_fields": {"request_id":request["request_id"], "proposal_kind":kind,
            "proposal_hash":proposal_hash,"checked_anchor_ids":anchors,"checked_item_ids":items},
    }

    def validate(response):
        if (
            response.request_id != request["request_id"]
            or response.proposal_kind != kind
            or response.proposal_hash != proposal_hash
        ):
            raise ValueError("Wrong source-review proposal binding")
        if response.outcome in {"ACCEPT", "REVISE"}:
            exact_ids(response.checked_anchor_ids, anchors)
            exact_ids(response.checked_item_ids, items)
        if response.outcome=='REVISE' and response.corrected_proposal is not None and proposal_validator is not None:
            proposal_validator(response.corrected_proposal.payload)
        for issue in response.issues:
            if not set(issue.evidence_anchor_ids) <= set(anchors):
                raise ValueError("Unknown source-review witness")

    name = {
        "INVENTORY": "source_inventory",
        "CARD_BATCH": "source_card",
        "TABLE_LAYOUT": "source_table_layout",
    }[kind]
    response = ops.request(
        "SECONDARY", name + "_review", data, SourceReviewResponse, validate=validate, source_limit=source_limit
    )
    if response.outcome == "ACCEPT":
        return proposal
    if response.outcome in {"REVISE", "SOURCE_UNRESOLVED"}:
        final = ops.request(
            "ARBITER",
            name + "_reconcile",
            {**data, "independent_review": response.model_dump(mode="json")},
            SourceReviewResponse,
            validate=validate,source_limit=source_limit,
        )
        if final.outcome == "ACCEPT":
            return proposal
        if final.outcome == "REVISE" and final.corrected_proposal is not None:
            return final.corrected_proposal.payload
        response = final
    if response.outcome=='NEED_EVIDENCE' and allow_evidence_requests:
        raise SourceContextRequested(response.evidence_requests)
    raise Hold(
        "SOURCE",
        "Unresolved source-only "
        + kind
        + ": "
        + str(response.unresolved_reason or response.outcome),
    )


def validate_frame(frame, packet):
    if (
        frame.source_packet_sha256 != sha(canonical(packet))
        or frame.paper_id != packet.paper_id
    ):
        raise ValueError("Source frame/packet binding mismatch")
    documents = {d.document_uid: d for d in packet.documents}
    anchors = {a.anchor_id: a for a in frame.anchors}
    tiles = {t.tile_id: t for t in frame.tiles}
    tables = {t.table_uid: t for t in frame.tables}
    if (
        len(anchors) != len(frame.anchors)
        or len(tiles) != len(frame.tiles)
        or len(tables) != len(frame.tables)
    ):
        raise ValueError("Duplicate source-frame identity")
    for anchor in anchors.values():
        doc = documents.get(anchor.document_uid)
        if doc is None or doc.file is None or anchor.source_sha256 != doc.file.sha256:
            raise ValueError("Anchor not bound to actual supplied source bytes")
    owned = set()
    for tile in tiles.values():
        for key in tile.owned_anchor_ids:
            if key not in anchors or key in owned:
                raise ValueError("Missing/overlapping owned source anchor")
            owned.add(key)
        if not set(tile.context_anchor_ids) <= anchors.keys():
            raise ValueError("Unknown context anchor")
        if tile.table_uid is not None and tile.table_uid not in tables:
            raise ValueError("Unknown source table")
    for table in tables.values():
        if table.numerical and (
            table.data_row_count <= 0 or table.layout_receipt is None
        ):
            raise ValueError(
                "Numerical table needs complete independently reviewed row layout"
            )
        if (
            len(table.data_row_anchor_ids) != table.data_row_count
            or len(set(table.data_row_anchor_ids)) != table.data_row_count
        ):
            raise ValueError("Source row-count mismatch")
        if (
            not set(
                table.data_row_anchor_ids
                + table.anchor_ids
                + table.header_anchor_ids
                + table.note_anchor_ids
            )
            <= anchors.keys()
        ):
            raise ValueError("Unknown table anchor")
    if not set(frame.excluded_region_anchor_ids) <= anchors.keys():
        raise ValueError("Unknown excluded source region")
    if frame.state != "READY" or packet.state != "READY":
        raise Hold("SOURCE", "Source packet/frame not ready")


class SourcePreparation:
    """Own source-only operations before outputs are inspected; all artifacts retained."""

    def __init__(self, ops, packet, anchors, parser_config_sha256):
        self.ops = ops
        self.packet = packet
        self.anchors = {a.anchor_id: a for a in anchors}
        self.parser_hash = parser_config_sha256
        self.inventory = []
        self.known_tables = {}
        self.layouts = []

    def evidence(self, ids):
        anchors=[self.anchors[k] for k in ids]
        result=[]
        for a in anchors:
            covered=False
            if a.rendering is not None and a.locator.kind=="page":
                x0,y0,x1,y1=a.locator.bbox
                for b in anchors:
                    if b is a or b.rendering is None or b.locator.kind!="page" or b.document_uid!=a.document_uid or b.locator.page_index!=a.locator.page_index:continue
                    p,q,r,s=b.locator.bbox
                    if p<=x0 and q<=y0 and r>=x1 and s>=y1 and (r-p)*(s-q)>(x1-x0)*(y1-y0):covered=True;break
            result.append(a.model_copy(update={"rendering":None}).model_dump(mode="json") if covered and a.exact_text is not None else a.model_dump(mode="json"))
        return result

    def inventory_page(self, ids, *, context=()):
        request_id = "inventory-" + sha(canonical([self.packet, ids, list(context)]))
        request = {
            "request_id": request_id,
            "source_manifest": self.packet.model_dump(mode="json"),
            "anchors": self.evidence(ids),
            "context": self.evidence(context),
            "verified_source_tables": self.known_tables,
            "inspection_anchor_ids":list(dict.fromkeys(ids+list(context))),
            "owned_scope_anchor_ids":ids,
            "context_only_anchor_ids":list(context),
        }

        def validate(response):
            if response.request_id != request_id:
                raise ValueError("Wrong inventory request")
            exact_ids(response.inspected_anchor_ids, list(dict.fromkeys(ids+list(context))))
            if response.outcome != "READY":
                raise Hold("SOURCE", "Source inventory unresolved")
            covered = {a for r in response.regions for a in r.anchor_ids}
            if not set(ids)<=covered or not covered<=set(ids+list(context)):
                raise ValueError("Inventory leaves unaccounted source scope: every owned image/text ID must occur in regions.anchor_ids. Missing owned IDs=" + repr(sorted(set(ids)-covered)) + "; unknown region IDs=" + repr(sorted(covered-set(ids+list(context)))))
            for r in response.regions:
                if r.table_uid is not None and (r.table_uid not in self.known_tables or self.known_tables[r.table_uid]["document_uid"]!=r.document_uid):
                    raise ValueError("Unverified source-table identity")
                if r.document_uid not in {
                    self.anchors[k].document_uid for k in r.anchor_ids
                }:
                    raise ValueError("Wrong document identity")

        response = self.ops.request(
            "SOURCE_READER",
            "source_inventory",
            request,
            InventoryResponse,
            validate=validate,
            source_tokens=self.ops.tokenizers["SOURCE_READER"](
                canonical(request).decode()
            ),
        )
        response = reviewed(
            self.ops,
            "INVENTORY",
            response,
            request,
            list(dict.fromkeys(ids+list(context))),
            [str(i) for i in range(len(response.regions))],
        )
        validate(response)
        original_ref=self.ops.store.put(response)
        local_keys={};assigned=[]
        for region in response.regions:
            if region.kind!="TABLE":assigned.append(region);continue
            uid=region.table_uid
            if uid is None:
                key=region.proposed_table_key
                if key not in local_keys:
                    local_keys[key]="table-"+sha(canonical([region.document_uid,region.source_regions,region.anchor_ids]))
                uid=local_keys[key]
            known=self.known_tables.setdefault(uid,{"document_uid":region.document_uid,"source_label":region.source_label,"anchor_ids":[]})
            known["anchor_ids"]=list(dict.fromkeys(known["anchor_ids"]+region.anchor_ids))
            assigned.append(region.model_copy(update={"table_uid":uid,"proposed_table_key":None}))
        response=response.model_copy(update={"regions":assigned})
        assigned_ref=self.ops.store.put(response)
        self.ops.store.put({"source_inventory":original_ref,"assigned_inventory":assigned_ref,"controller_table_identity_map":local_keys})
        self.inventory.append(assigned_ref)
        return response

    def layout(self, table_uid, document_uid, parts, context):
        """Page/region chunks; full header/note context retained at every owned request."""
        responses = []
        requests = []
        rows = []
        headers = []
        notes = []
        columns = {}
        part_layouts = {}
        all_context=list(context)
        from collections import deque
        pending=deque((p,list(p.anchor_ids),0,[]) for p in parts)
        while pending:
            part,ids,depth,extra_context=pending.popleft()
            pages={self.anchors[k].locator.page_index for k in ids if self.anchors[k].locator.kind=="page"}
            physical={k for p in parts for k in p.anchor_ids}
            def nearby(key):
                if key in physical and key not in ids:return False
                anchor=self.anchors[key]
                if anchor.locator.kind!='page':return True
                if anchor.locator.page_index not in pages:return False
                a,b,c,d=anchor.locator.bbox
                return any(self.anchors[k].locator.kind=='page' and
                    max(a,self.anchors[k].locator.bbox[0])<min(c,self.anchors[k].locator.bbox[2]) and
                    max(b,self.anchors[k].locator.bbox[1])<min(d,self.anchors[k].locator.bbox[3]) for k in ids)
            context=list(dict.fromkeys(headers+notes+extra_context+[k for k in all_context if nearby(k)]))
            request = SourceTableLayoutRequest(
                request_id="layout-" + sha(canonical([table_uid, part,ids,depth,context])),
                table_uid=table_uid,
                document_uid=document_uid,
                chunk_id=part.part_key+"-"+sha(canonical([ids,depth]))[:16],
                requested_scope_anchor_ids=ids,
                context_anchor_ids=list(context),
                part_manifest=parts,
                split_depth=depth,
            )
            data = {
                "request_id": request.request_id,
                "request": request.model_dump(mode="json"),
                "anchors": self.evidence(list(dict.fromkeys(ids + list(context)))),
            }

            def validate(response):
                if (
                    response.request_id,
                    response.table_uid,
                    response.chunk_id,
                    response.requested_scope_anchor_ids,
                ) != (request.request_id, table_uid, request.chunk_id, ids):
                    raise ValueError("Wrong layout chunk binding")
                exact_ids(
                    response.inspected_anchor_ids,
                    list(dict.fromkeys(ids + list(context))),
                )
                if response.outcome != "READY":
                    raise Hold("SOURCE", "Incomplete table layout")
                layout=response.layout
                if layout.document_uid!=document_uid:raise ValueError("Wrong table-layout document")
                allowed=set(ids+list(context));part_keys={p.part_key for p in parts}
                if len({p.part_key for p in layout.parts})!=len(layout.parts):raise ValueError("Duplicate table part")
                if any(p.part_key not in part_keys for p in layout.parts):raise ValueError("Unknown table part")
                column_keys=[(c.part_key,c.local_key) for c in layout.columns]
                if len(set(column_keys))!=len(column_keys):raise ValueError("Duplicate source column")
                for p in layout.parts:
                    if p.column_keys!=["c"+str(i+1).zfill(4) for i in range(len(p.column_keys))]:raise ValueError("Source column ordinals must be complete and ordered")
                    if any((p.part_key,key) not in column_keys for key in p.column_keys):raise ValueError("Missing declared source column")
                cited=set(layout.identity_evidence_anchor_ids+layout.header_anchor_ids+layout.note_anchor_ids)
                for c in layout.columns:
                    if c.part_key not in part_keys:raise ValueError("Column has unknown source part")
                    cited.update(a for h in c.header_path for a in h.anchor_ids)
                for r in layout.rows:cited.update(r.row_label_anchor_ids+r.context_anchor_ids+[s.anchor_id for s in r.row_regions])
                for r in layout.excluded_regions:cited.update(r.anchor_ids)
                if not cited<=allowed:raise ValueError("Table layout cites an unavailable anchor")
                for row in response.layout.rows:
                    first=row.row_regions[0]
                    origin=self.anchors[first.anchor_id]
                    inside=first.anchor_id in ids
                    if origin.locator.kind=='page':
                        x,y,_,_=first.bbox or origin.locator.bbox
                        inside=any(self.anchors[k].locator.kind=='page' and self.anchors[k].locator.page_index==origin.locator.page_index and
                            self.anchors[k].locator.bbox[0]<=x<self.anchors[k].locator.bbox[2] and self.anchors[k].locator.bbox[1]<=y<self.anchors[k].locator.bbox[3] for k in ids)
                    elif origin.locator.kind=='text':
                        inside=any(self.anchors[k].locator.kind=='text' and self.anchors[k].locator.start<=origin.locator.start<self.anchors[k].locator.stop for k in ids)
                    if not inside:raise ValueError('Source data row belongs to another owned layout chunk')
                    if row.part_key not in {p.part_key for p in parts}:
                        raise ValueError("Unknown table part")
                    for region in row.row_regions:
                        if region.anchor_id not in self.anchors:
                            raise ValueError("Unknown row geometry anchor")

            try:
                response = self.ops.request(
                    "SOURCE_READER",
                    "source_table_layout",
                    data,
                    SourceTableLayoutResponse,
                    validate=validate,
                    source_tokens=self.ops.tokenizers["SOURCE_READER"](
                        canonical(data).decode()
                    ),
                )
            except Hold as exc:
                size_failure='Source scope exceeds' in str(exc)
                if not (size_failure or isinstance(exc,LayoutSubdivisionRequired)) or depth>=8:
                    raise
                if len(ids)>1:
                    mid=len(ids)//2
                    children=[ids[:mid],ids[mid:]]
                else:
                    from .parsing import split_page_anchor
                    anchor=self.anchors[ids[0]]
                    if anchor.locator.kind!='page' or anchor.rendering is None:
                        raise Hold('SOURCE','Layout cannot be completely subdivided within source bounds') from exc
                    split=split_page_anchor(self.ops.store,anchor)
                    for child in split:self.anchors[child.anchor_id]=child
                    children=[[child.anchor_id] for child in split]
                for group in reversed(children):
                    extra=list(dict.fromkeys(headers+notes))
                    if len(ids)==1 and self.anchors[group[0]].locator.kind=='page':
                        from .parsing import crop_anchor
                        a,b,c,d=self.anchors[ids[0]].locator.bbox
                        x0,y0,x1,y1=self.anchors[group[0]].locator.bbox
                        margin=(y1-y0)/8
                        nearby_image=crop_anchor(self.ops.store,self.anchors[ids[0]],(x0,max(b,y0-margin),x1,min(d,y1+margin)))
                        self.anchors[nearby_image.anchor_id]=nearby_image
                        extra.append(nearby_image.anchor_id)
                    pending.appendleft((part,group,depth+1,extra))
                continue
            response = reviewed(
                self.ops,
                "TABLE_LAYOUT",
                response,
                data,
                list(dict.fromkeys(ids + list(context))),
                [part.part_key],
            )
            validate(response)
            requests.append(self.ops.store.put(request))
            responses.append(self.ops.store.put(response))
            layout = response.layout
            part_layouts.update({p.part_key:p for p in layout.parts})
            rows.extend(layout.rows)
            headers.extend(layout.header_anchor_ids+layout.identity_evidence_anchor_ids)
            notes.extend(layout.note_anchor_ids)
            for col in layout.columns:
                key = (col.part_key, col.local_key)
                if key in columns and columns[key] != col:
                    raise Hold("SOURCE", "Inconsistent continued-table header")
                columns[key] = col
        if not rows:
            raise Hold("SOURCE", "Numerical table has no verified data rows")
        row_ids = []
        for i, row in enumerate(rows):
            rowid = "row-" + sha(canonical([table_uid, i, row]))
            row_text,row_image=captured_row(self,row)
            self.anchors[rowid] = SourceAnchor(
                anchor_id=rowid,
                document_uid=document_uid,
                source_sha256=self.anchors[row.row_regions[0].anchor_id].source_sha256,
                locator=TableLocator(
                    kind="table",
                    table_uid=table_uid,
                    first_row=i,
                    stop_row=i + 1,
                    column_names=[],
                ),
                exact_text=row_text,
                rendering=row_image,
            )
            row_ids.append(rowid)
        logical = []
        logical_by_key={}
        for key,col in columns.items():
            continuation=part_layouts[key[0]].continuation_of_part_key
            prior=logical_by_key.get((continuation,key[1])) if continuation is not None else None
            member=SourceColumnMember(part_key=key[0],local_key=key[1])
            if prior is not None and (prior.role!=col.role or prior.header_path!=[h.text for h in col.header_path]):
                prior=None  # A continuation may introduce additional, distinct source columns.
            if prior is not None:
                updated=prior.model_copy(update={'source_columns':[*prior.source_columns,member]})
                logical[logical.index(prior)]=updated
                for k,v in list(logical_by_key.items()):
                    if v==prior:logical_by_key[k]=updated
                logical_by_key[key]=updated
            else:
                item=SourceLogicalColumn(column_uid='col-'+sha(canonical([table_uid,key])),role=col.role,
                    header_path=[h.text for h in col.header_path],source_columns=[member])
                logical.append(item);logical_by_key[key]=item
        receipt = SourceTableLayoutReceipt(
            table_uid=table_uid,
            document_uid=document_uid,
            source_packet_sha256=sha(canonical(self.packet)),
            chunk_requests=requests,
            chunk_responses=responses,
            source_reviews=[self.ops.response_refs[k] for k in self.ops.response_refs if __import__("json").loads(self.ops.store.get_blob(k))["operation"]=="source_table_layout_review"],
            arbiter_responses=[self.ops.response_refs[k] for k in self.ops.response_refs if __import__("json").loads(self.ops.store.get_blob(k))["operation"]=="source_table_layout_reconcile"],
            ordered_data_row_anchor_ids=row_ids,
            logical_columns=logical,
            header_anchor_ids=sorted(set(headers)),
            note_anchor_ids=sorted(set(notes)),
        )
        receipt_ref = self.ops.store.put(receipt)
        self.layouts.append(receipt_ref)
        return SourceTable(
            table_uid=table_uid,
            document_uid=document_uid,
            anchor_ids=list(dict.fromkeys(a for p in parts for a in p.anchor_ids)),
            numerical=True,
            data_row_count=len(rows),
            layout_receipt=receipt_ref,
            data_row_anchor_ids=row_ids,
            logical_column_names=[c.column_uid for c in logical],
            header_anchor_ids=sorted(set(headers)),
            note_anchor_ids=sorted(set(notes)),
        )


class SourceContextRequested(Exception):
    def __init__(self, requests):self.requests=requests


SOURCE_REQUIREMENT_POLICY = 'Preserve each coherent parent card and all its required scientific information. Within a card, use a distinct requirement for each independently reported condition-specific quantity or test. Do not bundle several independent times, groups or contrasts into one requirement merely because one sentence reports them together, including qualitative significance/null statements spanning independent comparisons. Keep interval endpoints in one interval requirement, and retain genuinely joint/multivariate findings as joint. Each independent effect/estimate and its uncertainty or p-value must be assignable to the same scientific review group. This changes requirement granularity within the parent card, not source facts or scoring weights. Independently review this equivalence against the source.'


def selected_cards(ops, tile, binding, evidence, *, expand=None):
    """Build a source target, with up to two expansions of captured source context."""
    run_start=len(getattr(ops,'run_refs',[]))
    def cards(batch):
        return [TaskCard(task_id='task-'+sha(canonical([tile.tile_id,card])),task_kind='CONTENT',binding=binding,
            tile_ids=[tile.tile_id],output_alias=None,source_bucket_id=None,scope_kind=card.scope_kind,requirements=card.requirements,
            evidence_anchor_ids=card.evidence_anchor_ids,source_scope_note=card.source_scope_note) for card in batch.cards]
    def defect(batch):
        from .source_recovery import SourceInventoryDefect,retain_defect
        raise SourceInventoryDefect(retain_defect(ops,tile,binding,batch,run_start))
    for expansion in range(3):
        limit=SOURCE_TOKEN_BUDGETS[expansion]
        request={"request_id":"cards-"+sha(canonical([tile,binding])),
            "tile":tile.model_dump(mode="json"),"tile_id":tile.tile_id,
            "requested_task_kind":"CONTENT","controller_requirement_policy":SOURCE_REQUIREMENT_POLICY,
            "owned_anchor_ids":tile.owned_anchor_ids,
            "context_only_anchor_ids":[a['anchor_id'] for a in evidence['anchors'] if a['anchor_id'] not in tile.owned_anchor_ids],
            "authoritative_evidence":evidence}
        anchor_ids=[a['anchor_id'] for a in evidence['anchors']]
        def validate(batch):
            if batch.tile_id!=tile.tile_id:raise ValueError('Wrong selected source tile')
            if batch.outcome!='READY':return
            if len({c.local_key for c in batch.cards})!=len(batch.cards):raise ValueError('Duplicate card key')
            for card in batch.cards:
                if card.task_kind!='CONTENT':raise ValueError('Source-card requests accept CONTENT only')
                keys=[r.requirement_id for r in card.requirements]
                if len(keys)!=len(set(keys)):raise ValueError('Duplicate source requirement identity')
                cited=set(card.evidence_anchor_ids)
                for requirement in card.requirements:cited.update(requirement.evidence_anchor_ids)
                if not cited<=set(anchor_ids):raise ValueError('Source card cites an unavailable source anchor')
                if not cited&set(tile.owned_anchor_ids):raise ValueError('A source card must concern the original owned tile, not only expanded context')
        batch=ops.request('SOURCE_READER','source_card',request,SourceTaskBatch,validate=validate,source_limit=limit)
        try:
            if batch.outcome=='NEED_EVIDENCE':raise SourceContextRequested(batch.evidence_requests)
            if batch.outcome in {'INVENTORY_DEFECT','SOURCE_UNRESOLVED'}:
                # An unreviewed source-reader decision cannot change sampling.
                batch=reviewed(ops,'CARD_BATCH',batch,{**request,'original_source_draft':batch.model_dump(mode='json')},
                    anchor_ids,['tile:'+tile.tile_id],source_limit=limit,allow_evidence_requests=True,proposal_validator=validate)
                if batch.outcome=='INVENTORY_DEFECT':defect(batch)
                if batch.outcome=='READY':return cards(batch)
            if batch.outcome!='READY':raise Hold('SOURCE','Selected tile requires common correction: '+batch.outcome)
            original=batch
            def cited(cards):
                return {a for c in cards for a in c.evidence_anchor_ids+[a for r in c.requirements for a in r.evidence_anchor_ids]}
            required_anchors=sorted(cited(original.cards))
            request={**request,'original_source_draft':original.model_dump(mode='json'),
                'required_preserved_evidence_anchor_ids':required_anchors}
            def validate_consolidated(proposal):
                validate(proposal)
                if proposal.outcome!='READY':return
                missing=set(required_anchors)-cited(proposal.cards)
                if missing:
                    raise ValueError('Source consolidation dropped original evidence anchors: '+repr(sorted(missing)))
            batch=ops.request('SOURCE_READER','source_card_consolidate',request,SourceTaskBatch,
                validate=validate_consolidated,source_limit=limit)
            if batch.outcome=='NEED_EVIDENCE':raise SourceContextRequested(batch.evidence_requests)
            if batch.outcome!='READY':raise Hold('SOURCE','Source consolidation unresolved: '+batch.outcome)
            request={**request,'source_consolidation_draft':batch.model_dump(mode='json')}
            batch=ops.request('SOURCE_READER','source_card_atomize',request,SourceTaskBatch,
                validate=validate_consolidated,source_limit=limit)
            if batch.outcome=='NEED_EVIDENCE':raise SourceContextRequested(batch.evidence_requests)
            check_items=['original:'+c.local_key for c in original.cards]+['proposed:'+c.local_key for c in batch.cards]
            batch=reviewed(ops,'CARD_BATCH',batch,request,anchor_ids,check_items,
                source_limit=limit,allow_evidence_requests=True,proposal_validator=validate)
            if batch.outcome=='INVENTORY_DEFECT':defect(batch)
            if batch.outcome!='READY':raise Hold('SOURCE','Selected tile requires common correction: '+batch.outcome)
            validate(batch)
            return cards(batch)
        except SourceContextRequested as need:
            if expand is None or expansion==2:raise Hold('SOURCE','Source-task context remains unresolved after permitted evidence expansions')
            evidence=expand(evidence,need.requests,SOURCE_TOKEN_BUDGETS[1:][expansion])
    raise Hold('SOURCE','Source-task evidence expansion limit exhausted')


def text_anchors(document, raw):
    """Frozen complete sentence spans for supplied UTF-8 text; PDF parsing is a separate derivative."""
    import re
    from .contracts import TextLocator

    text = raw.decode("utf-8", errors="strict")
    anchors = []
    # Freeze every span, including whitespace, so inventory cannot silently omit source material.
    for ordinal, match in enumerate(
        re.finditer(r".+?(?:[.!?](?=\s|$)|\n|$)", text, re.S)
    ):
        if not match.group().strip():
            continue
        start, stop = match.span()
        # Oversized sentences split only at whitespace boundaries at word 400.
        words = list(re.finditer(r"\S+", match.group()))
        spans = []
        for i in range(0, len(words), 400):
            a = start + (0 if i == 0 else words[i].start())
            b = stop if i + 400 >= len(words) else start + words[i + 400].start()
            spans.append((a, b))
        for a, b in spans:
            aid = "anchor-" + sha(
                canonical(
                    [document.file.sha256, ordinal, a, b, sha(text[a:b].encode())]
                )
            )
            anchors.append(
                SourceAnchor(
                    anchor_id=aid,
                    document_uid=document.document_uid,
                    source_sha256=document.file.sha256,
                    locator=TextLocator(kind="text", start=a, stop=b),
                    exact_text=text[a:b],
                    rendering=None,
                )
            )
    return anchors


def assemble_frame(preparation, inventories, *, revision=0):
    """Controller tiling from independently accepted regions and verified whole-table layouts."""
    from .contracts import SourceTile, SourceFrame

    prose = []
    excluded = []
    tables = {}
    table_eligible = {}
    region_seen = set()
    run = []
    run_words = 0

    def flush():
        nonlocal run, run_words
        if run:
            prose.append(
                SourceTile(
                    tile_id="tile-" + sha(canonical(run)),
                    document_uid=preparation.anchors[run[0]].document_uid,
                    stratum="PROSE_OR_EQUATION",
                    eligible_content=True,
                    owned_anchor_ids=run,
                    context_anchor_ids=[],
                    table_uid=None,
                    first_data_row=None,
                    stop_data_row=None,
                )
            )
        run = []
        run_words = 0

    for inventory in inventories:
        if inventory.outcome != "READY":
            raise Hold("SOURCE", "Inventory is not resolved")
        for region in inventory.regions:
            if region.source_regions:
                derived=[]
                for piece in region.source_regions:
                    original=preparation.anchors[piece.anchor_id]
                    if piece.bbox is not None:
                        if original.locator.kind!='page':raise Hold('SOURCE','Region geometry requires original page anchor')
                        from .parsing import crop_anchor
                        cropped=crop_anchor(preparation.ops.store,original,piece.bbox)
                        preparation.anchors[cropped.anchor_id]=cropped
                        derived.append(cropped.anchor_id)
                    else:derived.append(piece.anchor_id)
                region=region.model_copy(update={'anchor_ids':derived})
            if set(region.anchor_ids) & region_seen:
                raise Hold(
                    "SOURCE",
                    "Inventory regions overlap; need source-region subdivision",
                )
            region_seen.update(region.anchor_ids)
            # Distinct source sections and excluded blocks delimit prose runs.
            if region.kind == "EXCLUDED":
                flush()
                excluded.extend(region.anchor_ids)
                continue
            if region.kind == "TABLE":
                flush()
                key = region.table_uid or "table-" + sha(
                    canonical([inventory.request_id, region.proposed_table_key])
                )
                tables.setdefault(key, []).append(region)
                table_eligible[key] = (
                    table_eligible.get(key, False) or region.has_extractable_science
                )
                continue
            if not region.has_extractable_science:
                flush()
                excluded.extend(region.anchor_ids)
                continue
            for aid in region.anchor_ids:
                anchor = preparation.anchors[aid]
                words = len((anchor.exact_text or "").split())
                if words > 400:
                    raise Hold(
                        "SOURCE",
                        "Parser anchor exceeds tile word bound; source subdivision required",
                    )
                if anchor.exact_text is None:
                    flush()
                if run and (
                    run_words + words > 400
                    or preparation.anchors[run[0]].document_uid != anchor.document_uid
                ):
                    flush()
                run.append(aid)
                run_words += words
                if anchor.exact_text is None:
                    flush()
    flush()
    all_tables = []
    tiles = list(prose)
    for tableid, regions in tables.items():
        doc = regions[0].document_uid
        if any(r.document_uid != doc for r in regions):
            raise Hold("SOURCE", "Cross-document table identity unresolved")
        parts = [
            SourcePartIdentity(
                part_key="part-" + str(i), source_order=i, anchor_ids=r.anchor_ids
            )
            for i, r in enumerate(regions)
        ]
        # Complete captured region evidence is retained in every chunk; layout identifies exact rows/notes.
        context = list(dict.fromkeys(a for r in regions for a in r.anchor_ids))
        pages={preparation.anchors[k].locator.page_index for k in context if preparation.anchors[k].locator.kind=="page"}
        context.extend(a.anchor_id for a in preparation.anchors.values() if a.document_uid==doc and a.exact_text is not None and a.locator.kind=="page" and a.locator.page_index in pages)
        context=list(dict.fromkeys(context))
        table = preparation.layout(tableid, doc, parts, context)
        all_tables.append(table)
        for i in range(0, table.data_row_count, 5):
            owned = table.data_row_anchor_ids[i : i + 5]
            tiles.append(
                SourceTile(
                    tile_id=tableid + "-rows-" + str(i),
                    document_uid=doc,
                    stratum="TABLE",
                    eligible_content=table_eligible[tableid],
                    owned_anchor_ids=owned,
                    context_anchor_ids=list(
                        dict.fromkeys(
                            table.header_anchor_ids
                            + table.note_anchor_ids
                        )
                    ),
                    table_uid=tableid,
                    first_data_row=i,
                    stop_data_row=min(i + 5, table.data_row_count),
                )
            )
    return SourceFrame(
        protocol_id="claims-source-task-v1",
        paper_id=preparation.packet.paper_id,
        source_packet_sha256=sha(canonical(preparation.packet)),
        parser_config_sha256=preparation.parser_hash,
        revision=revision,
        state="READY",
        anchors=list(preparation.anchors.values()),
        tables=all_tables,
        tiles=tiles,
        excluded_region_anchor_ids=excluded,
        errors=[],
    )


def captured_row(preparation,row):
    """Derive readable row evidence from original pixels and captured parser cells only."""
    from .parsing import crop_anchor
    regions=[]
    for piece in row.row_regions:
        anchor=preparation.anchors[piece.anchor_id]
        if anchor.locator.kind=='page':regions.append((anchor.locator.page_index,piece.bbox or anchor.locator.bbox))
    if not regions:
        anchor=preparation.anchors[row.row_regions[0].anchor_id]
        return anchor.exact_text,anchor.rendering
    page=min(p for p,_ in regions)
    boxes=[b for p,b in regions if p==page]
    union=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
    captured=[];images=[]
    for anchor in preparation.anchors.values():
        if anchor.document_uid!=preparation.anchors[row.row_regions[0].anchor_id].document_uid or anchor.locator.kind!='page' or anchor.locator.page_index!=page:continue
        x0,y0,x1,y1=anchor.locator.bbox;a,b,c,d=union
        if anchor.exact_text is not None and a<=(x0+x1)/2<=c and b<=(y0+y1)/2<=d:captured.append(anchor)
        if anchor.rendering is not None and x0<=a and y0<=b and x1>=c and y1>=d:images.append(anchor)
    text=' '.join(a.exact_text for a in sorted(captured,key=lambda a:(a.locator.bbox[1],a.locator.bbox[0],a.anchor_id))) or None
    image=None
    if images:
        parent=min(images,key=lambda a:(a.locator.bbox[2]-a.locator.bbox[0])*(a.locator.bbox[3]-a.locator.bbox[1]))
        image=crop_anchor(preparation.ops.store,parent,union).rendering
    return text,image
