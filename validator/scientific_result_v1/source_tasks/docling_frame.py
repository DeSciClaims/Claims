"""Controller-owned Docling PDF evidence; no page images or model-authored geometry.

Owner policy, 21 November 2026: Docling's independently parsed text/OCR/table
structure is the source evidence. Parser omissions are a disclosed limitation.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import time

from .contracts import (
    Closed, FileRef, SourceAnchor, SourceFrame, SourceTable, SourceTile,
    PageLocator, TextLocator, TableLocator,
)
from .store import canonical, sha
from .operations import Hold

POLICY = 'docling_pdf_derivatives_v1'
FRAME_KIND = 'claims_docling_source_frame_v1'
LAYOUT_KIND = 'claims_docling_table_layout_v1'


class EligibilityItem(Closed):
    tile_id: str
    eligible: bool
    reason: str


class EligibilityResponse(Closed):
    items: list[EligibilityItem]


def _parts(text):
    """Frozen complete sentences, splitting oversized sentences at word 400."""
    for match in re.finditer(r'.+?(?:[.!?](?=\s|$)|\n|$)', text, re.S):
        if not match.group().strip():
            continue
        words = list(re.finditer(r'\S+', match.group()))
        for i in range(0, len(words), 400):
            start = match.start() + (0 if i == 0 else words[i].start())
            stop = match.end() if i + 400 >= len(words) else match.start() + words[i + 400].start()
            yield start, stop, text[start:stop]


def _locator(record, document, start, stop):
    for prov in record.get('prov', []):
        page = document.get('pages', {}).get(str(prov.get('page_no')))
        if not page:
            continue
        width, height = page['size']['width'], page['size']['height']
        box = prov.get('bbox', {})
        if not all(k in box for k in ('l', 't', 'r', 'b')):
            continue
        left, right = sorted((box['l'] / width, box['r'] / width))
        top, bottom = box['t'] / height, box['b'] / height
        if box.get('coord_origin') == 'BOTTOMLEFT':
            top, bottom = 1 - top, 1 - bottom
        top, bottom = sorted((top, bottom))
        bounds = tuple(max(0.0, min(1.0, float(v))) for v in (left, top, right, bottom))
        if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
            return PageLocator(kind='page', page_index=prov['page_no'] - 1, bbox=bounds)
    return TextLocator(kind='text', start=start, stop=max(start + 1, stop))


def _ordered_texts(document):
    seen = set()
    def visit(reference):
        if reference in seen:
            return
        seen.add(reference)
        bits = reference.removeprefix('#/').split('/')
        try:
            node = document[bits[0]]
            for bit in bits[1:]:
                node = node[int(bit)] if isinstance(node, list) else node[bit]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ValueError('Docling reading-order reference does not resolve: ' + reference)
        if reference.startswith('#/texts/'):
            yield int(bits[1])
        for child in node.get('children', []):
            yield from visit(child['$ref'])
        # Captions are sometimes referenced separately from children.
        for child in node.get('captions', []):
            yield from visit(child['$ref'])
    if document.get('body', {}).get('children'):
        yield from visit('#/body')
    for i in range(len(document.get('texts', []))):
        if '#/texts/' + str(i) not in seen:
            yield i


def _table_grid(table):
    data = table['data']
    cells = data.get('table_cells', [])
    rows, cols = data['num_rows'], data['num_cols']
    if not 0 < rows <= 200000 or not 0 < cols <= 10000 or rows * cols > 2000000:
        raise ValueError('Docling table dimensions exceed source limits')
    grid = [['' for _ in range(cols)] for _ in range(rows)]
    originals = [[] for _ in range(rows)]
    for index, cell in enumerate(cells):
        a, b = cell['start_row_offset_idx'], cell['end_row_offset_idx']
        c, d = cell['start_col_offset_idx'], cell['end_col_offset_idx']
        if not (0 <= a < b <= rows and 0 <= c < d <= cols):
            raise ValueError('Invalid original Docling cell span')
        originals[a].append((index, cell))
        for row in range(a, b):
            for column in range(c, d):
                value = cell.get('text', '')
                if grid[row][column] and grid[row][column] != value:
                    raise ValueError('Overlapping incompatible Docling cells')
                grid[row][column] = value
    header_rows, section_rows, data_rows, blank_rows = [], [], [], []
    for row, entries in enumerate(originals):
        if not entries or not any(v.strip() for v in grid[row]):
            blank_rows.append(row)
        elif any(c.get('column_header') for _, c in entries) and not any(
                not c.get('column_header') and not c.get('row_section') and c['start_col_offset_idx'] > 0
                for _, c in entries):
            header_rows.append(row)
        elif all(c.get('row_section') for _, c in entries):
            section_rows.append(row)
        else:
            data_rows.append(row)
    return grid, header_rows, section_rows, data_rows, blank_rows


def _capture(store, packet, directories):
    documents = []
    for number, source in enumerate(packet.documents, 1):
        if source.file is None:
            if source.required:
                raise Hold('SOURCE', 'Required supplied PDF is unavailable')
            continue
        raw = store.read(source.file)
        if source.file.media_type != 'application/pdf':
            raise ValueError('Docling frame accepts PDF source bundles only')
        cache = directories.get(source.document_uid)
        with tempfile.TemporaryDirectory(prefix='claims-validator-docling-') as work:
            directory = Path(cache) if cache is not None else Path(work) / 'parsed'
            stamp=directory/'validator_parse_run.json'
            trusted=False
            if stamp.exists():
                run=json.loads(stamp.read_text())
                trusted=run.get('independent_validator_parse') is True and run.get('source_sha256')==source.file.sha256
            if not trusted or not (directory / 'source_payload.json').exists():
                # Never accept a miner's parsed text/CSV merely because it names the PDF hash.
                # Existing untrusted caches are left unchanged; parse original bytes separately.
                if directory.exists() and (directory/'source_payload.json').exists():
                    directory=Path(work)/'independent-docling'
                from miner.scientific_result_v1.docling_source import parse_pdf
                original = Path(work) / 'source.pdf'
                original.write_bytes(raw)
                started=time.time()
                parse_pdf(original, directory)
                (directory/'validator_parse_run.json').write_text(json.dumps({
                    'source_sha256':source.file.sha256,'independent_validator_parse':True,
                    'started_at':started,'finished_at':time.time(),'source_evidence_policy':POLICY}))
            metadata_bytes = (directory / 'source_payload.json').read_bytes()
            metadata = json.loads(metadata_bytes)
            if metadata['source_sha256'] != source.file.sha256 or sha(raw) != source.file.sha256:
                raise ValueError('Docling PDF/cache source hash mismatch')
            if metadata['parser'].get('name') != 'docling' or not metadata['parser'].get('ocr'):
                raise ValueError('Validator PDF evidence requires Docling with OCR enabled')
            artifacts = {}
            for name in ('source_payload.json', 'docling_document.json', 'source.md', 'page_ocr.json', 'page_ocr.txt', 'validator_parse_run.json'):
                path = directory / name
                if path.is_file():
                    artifacts[name] = store.put_named(f'docling/d{number}/' + name, path.read_bytes(), 'application/json' if path.suffix == '.json' else 'text/plain').model_dump(mode='json')
            for table in metadata.get('tables', []):
                name = table['csv_file']
                if name.startswith('/') or '\\' in name or any(p in ('', '.', '..') for p in name.split('/')):
                    raise ValueError('Unsafe Docling CSV path')
                content = (directory / name).read_bytes()
                if sha(content) != table['sha256']:
                    raise ValueError('Original Docling table CSV hash mismatch')
                artifacts[name] = store.put_named(f'docling/d{number}/' + name, content, 'text/csv').model_dump(mode='json')
            documents.append({'document_uid': source.document_uid, 'source_sha256': source.file.sha256,
                              'artifacts': artifacts, 'parser': metadata['parser']})
    return documents


def _read(store, ref):
    return store.read(FileRef.model_validate(ref, strict=True))


def derive_frame(store, packet, config, config_sha256):
    """Pure replayable projection of retained Docling bytes; never reads a miner output."""
    anchors, tables, prose, excluded = [], [], [], []
    decisions = {d['tile_id']: d['eligible'] for d in config.get('eligibility', [])}
    for number, spec in enumerate(config['documents'], 1):
        document = json.loads(_read(store, spec['artifacts']['docling_document.json']))
        uid, pdf_hash = spec['document_uid'], spec['source_sha256']
        texts = document.get('texts', [])
        by_record = {}
        offset = 0
        for ordinal, record in enumerate(texts):
            text = record.get('text', '')
            by_record[ordinal] = []
            for part, (start, stop, value) in enumerate(_parts(text)):
                aid = f'd{number}-text{ordinal}-s{part}'
                anchor = SourceAnchor(anchor_id=aid, document_uid=uid, source_sha256=pdf_hash,
                    locator=_locator(record, document, offset + start, offset + stop), exact_text=value, rendering=None)
                anchors.append(anchor)
                by_record[ordinal].append(aid)
            offset += len(text) + 1
        # Independent OCR capture is available for checking parser/table discrepancies,
        # but is not another population of duplicate scientific result tiles.
        ocr_ref=spec['artifacts'].get('page_ocr.json')
        if ocr_ref is not None:
            for captured in json.loads(_read(store,ocr_ref)):
                page=captured['page']-1
                text='\n'.join(c.get('text','') for c in captured.get('cells',[]) if c.get('text','').strip())
                if text:
                    anchors.append(SourceAnchor(anchor_id=f'd{number}-ocr-page{page}',document_uid=uid,
                        source_sha256=pdf_hash,locator=PageLocator(kind='page',page_index=page,bbox=(0.0,0.0,1.0,1.0)),
                        exact_text=text,rendering=None))
        lookup = {a.anchor_id: a for a in anchors}
        table_caption_indices = {int(r['$ref'].split('/')[-1]) for t in document.get('tables', []) for r in t.get('captions', []) if r['$ref'].startswith('#/texts/')}
        sections, run, run_words, in_backmatter = [], [], 0, False
        def flush():
            nonlocal run, run_words
            if run:
                tile_id = f'd{number}-prose{sum(t.document_uid == uid for t in prose)}'
                prose.append(SourceTile(tile_id=tile_id, document_uid=uid, stratum='PROSE_OR_EQUATION',
                    eligible_content=decisions.get(tile_id, True), owned_anchor_ids=run,
                    context_anchor_ids=list(sections), table_uid=None, first_data_row=None, stop_data_row=None))
            run, run_words = [], 0
        for ordinal in _ordered_texts(document):
            record = texts[ordinal]
            label, text = record.get('label'), record.get('text', '')
            ids = by_record[ordinal]
            if label in ('section_header', 'title'):
                flush()
                sections = ids
                in_backmatter = bool(re.match(r'^(references|bibliography|acknowledg\w*|author contributions?|competing interests?|conflicts? of interest|funding|affiliations?)\b', text.strip(), re.I))
                excluded.extend(ids)
                continue
            if record.get('content_layer') == 'furniture' or label in ('page_header', 'page_footer') or in_backmatter or ordinal in table_caption_indices:
                flush()
                excluded.extend(ids)
                continue
            for aid in ids:
                words = len(lookup[aid].exact_text.split())
                if run and run_words + words > 400:
                    flush()
                run.append(aid)
                run_words += words
        flush()
        for index, table in enumerate(document.get('tables', [])):
            grid, header_rows, section_rows, data_rows, blank_rows = _table_grid(table)
            if not data_rows or not any(re.search(r'\d', value) for row in data_rows for value in grid[row]):
                continue
            table_uid = f'd{number}-table{index}'
            caption_ids = [aid for r in table.get('captions', []) if r['$ref'].startswith('#/texts/') for aid in by_record[int(r['$ref'].split('/')[-1])]]
            note_ids = [aid for r in table.get('footnotes', []) if r['$ref'].startswith('#/texts/') for aid in by_record[int(r['$ref'].split('/')[-1])]]
            # Retain all captured text on the table's pages as available context.
            table_pages = {p['page_no'] - 1 for p in table.get('prov', [])}
            note_ids += [a.anchor_id for a in anchors if a.document_uid == uid and a.locator.kind == 'page'
                         and a.locator.page_index in table_pages and a.anchor_id not in caption_ids]
            headers = list(caption_ids)
            for row in header_rows + section_rows:
                aid = f'd{number}-table{index}-context{row}'
                anchors.append(SourceAnchor(anchor_id=aid, document_uid=uid, source_sha256=pdf_hash,
                    locator=_locator(table, document, offset, offset + 1), exact_text=' | '.join(grid[row]), rendering=None))
                (headers if row in header_rows else note_ids).append(aid)
            row_ids, active_sections = [], []
            for row in range(len(grid)):
                if row in section_rows:
                    active_sections = [v for v in grid[row] if v]
                if row not in data_rows:
                    continue
                aid = f'd{number}-table{index}-row{row}'
                row_ids.append(aid)
                anchors.append(SourceAnchor(anchor_id=aid, document_uid=uid, source_sha256=pdf_hash,
                    locator=TableLocator(kind='table', table_uid=table_uid, first_row=len(row_ids)-1, stop_row=len(row_ids), column_names=[f'c{i+1}' for i in range(len(grid[row]))]),
                    exact_text=' | '.join(active_sections + grid[row]), rendering=None))
            if not headers:
                aid = f'd{number}-table{index}-identity'
                anchors.append(SourceAnchor(anchor_id=aid, document_uid=uid, source_sha256=pdf_hash,
                    locator=_locator(table, document, offset, offset+1), exact_text=json.dumps({'docling_table_ref': table['self_ref']}, ensure_ascii=False), rendering=None))
                headers.append(aid)
            proof = {'kind': LAYOUT_KIND, 'source_packet_sha256': sha(canonical(packet)), 'document_uid': uid,
                     'table_uid': table_uid, 'docling_document': spec['artifacts']['docling_document.json'],
                     'table_index': index, 'data_row_indices': data_rows, 'header_row_indices': header_rows,
                     'section_row_indices': section_rows, 'blank_row_indices': blank_rows,
                     'row_anchor_ids': row_ids, 'header_anchor_ids': list(dict.fromkeys(headers)),
                     'note_anchor_ids': list(dict.fromkeys(note_ids)), 'column_names': [f'c{i+1}' for i in range(len(grid[0]))]}
            tables.append(SourceTable(table_uid=table_uid, document_uid=uid,
                anchor_ids=list(dict.fromkeys(headers + note_ids + row_ids)), numerical=True,
                data_row_count=len(row_ids), layout_receipt=store.put(proof), data_row_anchor_ids=row_ids,
                logical_column_names=proof['column_names'], header_anchor_ids=proof['header_anchor_ids'], note_anchor_ids=proof['note_anchor_ids']))
    tiles = list(prose)
    for table in tables:
        for start in range(0, table.data_row_count, 5):
            tiles.append(SourceTile(tile_id=table.table_uid + '-rows' + str(start), document_uid=table.document_uid,
                stratum='TABLE', eligible_content=True, owned_anchor_ids=table.data_row_anchor_ids[start:start+5],
                context_anchor_ids=list(dict.fromkeys(table.header_anchor_ids + table.note_anchor_ids)),
                table_uid=table.table_uid, first_data_row=start, stop_data_row=min(start+5, table.data_row_count)))
    return SourceFrame(protocol_id=packet.protocol_id, paper_id=packet.paper_id,
        source_packet_sha256=sha(canonical(packet)), parser_config_sha256=config_sha256,
        revision=0, state='READY', anchors=anchors, tables=tables, tiles=tiles,
        excluded_region_anchor_ids=excluded, errors=[])


def _classify(ops, frame):
    by_id = {a.anchor_id: a for a in frame.anchors}
    proposed = []
    for tile in frame.tiles:
        if tile.stratum != 'PROSE_OR_EQUATION':
            continue
        proposed.append({'tile_id': tile.tile_id,
            'text': '\n'.join(by_id[k].exact_text for k in tile.owned_anchor_ids),
            'context': '\n'.join(by_id[k].exact_text for k in tile.context_anchor_ids)})
    accepted = []
    batches=[];batch=[]
    for tile in proposed:
        candidate=[*batch,tile]
        if batch and (len(candidate)>6 or max(counter(canonical(candidate).decode()) for counter in ops.tokenizers.values())>7800):
            batches.append(batch);batch=[]
        batch.append(tile)
    if batch:batches.append(batch)
    for batch in batches:
        data = {'source_evidence_policy': POLICY, 'tiles': batch}
        expected = {t['tile_id'] for t in batch}
        def validate(response):
            keys = [v.tile_id for v in response.items]
            if len(keys) != len(set(keys)) or set(keys) != expected:
                raise ValueError('Eligibility must classify each supplied tile exactly once: ' + repr(sorted(expected)))
        primary = ops.request('SOURCE_READER', 'source_eligibility', data, EligibilityResponse, validate=validate,
                              source_tokens=ops.tokenizers['SOURCE_READER'](canonical(batch).decode()))
        secondary = ops.request('SECONDARY', 'source_eligibility', data, EligibilityResponse, validate=validate,
                                source_tokens=ops.tokenizers['SECONDARY'](canonical(batch).decode()))
        flags = lambda r: {v.tile_id: v.eligible for v in r.items}
        decisive = primary
        if flags(primary) != flags(secondary):
            decisive = ops.request('ARBITER', 'source_eligibility', {**data, 'independent_decisions': [primary.model_dump(mode='json'), secondary.model_dump(mode='json')]}, EligibilityResponse, validate=validate,
                                   source_tokens=ops.tokenizers['ARBITER'](canonical(batch).decode()))
        accepted.extend(v.model_dump(mode='json') for v in decisive.items)
    return accepted


def prepare_docling_frame(store, packet, directories, ops, policy_sha256):
    documents = _capture(store, packet, directories)
    config = {'kind': FRAME_KIND, 'source_evidence_policy': POLICY,
              'source_packet_sha256': sha(canonical(packet)), 'policy_sha256': policy_sha256,
              'documents': documents, 'eligibility': [], 'eligibility_runs': []}
    draft = derive_frame(store, packet, config, '0' * 64)
    before = len(ops.run_refs)
    config['eligibility'] = _classify(ops, draft)
    config['eligibility_runs'] = [r.model_dump(mode='json') for r in ops.run_refs[before:]]
    ref = store.put(config)
    result = derive_frame(store, packet, config, ref.sha256)
    validate_docling_frame(store, result, packet)
    return result


def validate_docling_frame(store, frame, packet):
    """Replay the source frame and every retained Docling input without a model call."""
    try:
        config = json.loads(store.get_blob(frame.parser_config_sha256))
    except (KeyError, ValueError, UnicodeError):
        return False  # Legacy source-frame proof.
    if config.get('kind') != FRAME_KIND:
        return False
    if config['source_evidence_policy'] != POLICY or config['source_packet_sha256'] != sha(canonical(packet)):
        raise ValueError('Docling source frame has a different source/policy binding')
    supplied = {d.document_uid: d for d in packet.documents if d.file is not None}
    if {d['document_uid'] for d in config['documents']} != set(supplied):
        raise ValueError('Docling source evidence omits a supplied document')
    for d in config['documents']:
        source = supplied[d['document_uid']]
        if d['source_sha256'] != source.file.sha256 or sha(store.read(source.file)) != d['source_sha256']:
            raise ValueError('Docling evidence belongs to another PDF')
        for ref in d['artifacts'].values():
            _read(store, ref)
        metadata = json.loads(_read(store, d['artifacts']['source_payload.json']))
        if metadata['source_sha256'] != source.file.sha256 or metadata['parser'] != d['parser']:
            raise ValueError('Docling source metadata changed')
    draft=derive_frame(store,packet,{**config,'eligibility':[]},frame.parser_config_sha256)
    _validate_eligibility(store,config,draft)
    expected = derive_frame(store, packet, config, frame.parser_config_sha256)
    if expected != frame:
        raise ValueError('Docling frame differs from retained parser structure and eligibility decisions')
    return True


def validate_docling_layout(store, table, frame, packet):
    if table not in frame.tables:
        raise ValueError('Docling table is not part of the retained source frame')
    if not validate_docling_frame(store, frame, packet):
        raise ValueError('Docling table lacks a bound Docling source-frame proof')
    return True


def _validate_eligibility(store, config, draft):
    anchors={a.anchor_id:a for a in draft.anchors}
    proposed={t.tile_id:{'tile_id':t.tile_id,
        'text':'\n'.join(anchors[k].exact_text for k in t.owned_anchor_ids),
        'context':'\n'.join(anchors[k].exact_text for k in t.context_anchor_ids)}
        for t in draft.tiles if t.stratum=='PROSE_OR_EQUATION'}
    covered=set()
    from .contracts import JudgeRunReceipt
    successful = {}
    for ref in config['eligibility_runs']:
        run=store.load(FileRef.model_validate(ref,strict=True),JudgeRunReceipt)
        request=json.loads(store.read(run.request))
        base={k:v for k,v in request.items() if k!='validation_feedback'}
        if sha(canonical(base))!=run.run_id.rsplit(':',1)[0] or request['policy_sha256']!=config['policy_sha256']:
            raise ValueError('Docling eligibility request/policy binding changed')
        if request['operation']!='source_eligibility' or request['role']!=run.role:
            raise ValueError('Docling eligibility has a different operation/role')
        if run.error or run.parsed_response is None:
            continue
        response=store.load(run.parsed_response,EligibilityResponse)
        data=request['payload'];tiles=data['tiles']
        keys=[item.tile_id for item in response.items]
        if len(keys)!=len(set(keys)) or set(keys)!={tile['tile_id'] for tile in tiles}:
            raise ValueError('Eligibility response does not cover its source batch')
        if run.role=='SECONDARY' and 'independent_decisions' in data:
            raise ValueError('Docling eligibility secondary was not blind')
        if data.get('source_evidence_policy')!=POLICY or any(t!=proposed.get(t['tile_id']) for t in tiles):
            raise ValueError('Eligibility source text differs from original Docling output')
        covered.update(t['tile_id'] for t in tiles)
        key=sha(canonical(tiles))
        successful.setdefault(key,{})[run.role]=(response,request['route'])
    if covered!=set(proposed):raise ValueError('Docling eligibility omits source tiles')
    expected=[]
    for roles in successful.values():
        if not {'SOURCE_READER','SECONDARY'}<=roles.keys():
            raise ValueError('Docling eligibility lacks independent decisions')
        first,second=roles['SOURCE_READER'][0],roles['SECONDARY'][0]
        identities={(r['provider'],r['model'],r['runtime_revision']) for _,r in roles.values()}
        if len(identities)!=len(roles):
            raise ValueError('Docling eligibility model identities are not independent')
        flags=lambda response:{v.tile_id:v.eligible for v in response.items}
        decisive=first
        if flags(first)!=flags(second):
            if 'ARBITER' not in roles:raise ValueError('Docling eligibility disagreement lacks arbitration')
            decisive=roles['ARBITER'][0]
        expected.extend(v.model_dump(mode='json') for v in decisive.items)
    key=lambda item:item['tile_id']
    if sorted(expected,key=key)!=sorted(config['eligibility'],key=key):
        raise ValueError('Docling eligibility flags differ from retained decisions')
