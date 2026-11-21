"""Frozen Docling layout and original-page evidence for independent source inventory."""
from __future__ import annotations
import json
from pathlib import Path
from .contracts import SourceAnchor,PageLocator
from .store import canonical,sha


def pdf_anchors(store,document,pdf,parsed_directory):
    """Use retained Docling derivatives; render actual source pages, never candidate tables."""
    import fitz
    if sha(pdf)!=document.file.sha256:raise ValueError('Parser input differs from source packet')
    directory=Path(parsed_directory)
    metadata=json.loads((directory/'source_payload.json').read_text())
    if metadata['source_sha256']!=document.file.sha256:raise ValueError('Cached Docling input hash mismatch')
    parsed=json.loads((directory/'docling_document.json').read_text())
    derivatives={p.name:sha(p.read_bytes()) for p in directory.iterdir() if p.is_file()}
    parser_hash=sha(canonical({'metadata':metadata['parser'],'derivatives':derivatives,'render_dpi':144,'fitz':fitz.VersionBind}))
    anchors=[];pages=[]
    with fitz.open(stream=pdf,filetype='pdf') as source:
        for page_index,page in enumerate(source):
            png=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False).tobytes('png')
            image=store.put_named('source-images/'+sha(png)+'.png',png,'image/png')
            pageid='page-'+sha(canonical([document.file.sha256,page_index,sha(png)]))
            page_anchor=SourceAnchor(anchor_id=pageid,document_uid=document.document_uid,source_sha256=document.file.sha256,
                locator=PageLocator(kind='page',page_index=page_index,bbox=(0.0,0.0,1.0,1.0)),exact_text=None,rendering=image)
            anchors.append(page_anchor);ids=[]
            # Original Docling reading order/identities are retained; no miner organization is visible.
            records=list(parsed.get('texts',[]))
            for table in parsed.get('tables',[]):
                if len(table.get('prov',[]))!=1:continue
                for cell_index,cell in enumerate(table.get('data',{}).get('table_cells',[])):
                    if cell.get('bbox') is not None:
                        records.append({'self_ref':str(table.get('self_ref'))+'/cell/'+str(cell_index),
                            'text':cell.get('text',''),'prov':[{'page_no':table['prov'][0]['page_no'],'bbox':cell['bbox']}]})
            for record in records:
                for ordinal,provenance in enumerate(record.get('prov',[])):
                    if provenance.get('page_no')!=page_index+1:continue
                    box=provenance.get('bbox',{});left=float(box.get('l',0));right=float(box.get('r',page.rect.width))
                    top=float(box.get('t',0));bottom=float(box.get('b',page.rect.height))
                    if box.get('coord_origin')=='BOTTOMLEFT':top,bottom=page.rect.height-top,page.rect.height-bottom
                    bbox=(max(0.0,min(left,right)/page.rect.width),max(0.0,min(top,bottom)/page.rect.height),
                          min(1.0,max(left,right)/page.rect.width),min(1.0,max(top,bottom)/page.rect.height))
                    text=record.get('text','')
                    if not text.strip():continue
                    try:captured=crop_anchor(store,page_anchor,bbox).rendering
                    except ValueError:captured=image
                    aid='anchor-'+sha(canonical([document.file.sha256,page_index,record.get('self_ref'),ordinal,bbox,sha(text.encode()),captured.sha256]))
                    anchors.append(SourceAnchor(anchor_id=aid,document_uid=document.document_uid,source_sha256=document.file.sha256,
                        locator=PageLocator(kind='page',page_index=page_index,bbox=bbox),exact_text=text,rendering=captured));ids.append(aid)
            # One page per inventory request. Text records are context; the image owns
            # the complete page so figures/tables absent from OCR remain discoverable.
            pages.append({'owned_anchor_ids':[pageid],'context_anchor_ids':ids})
    store.put({'parser_config_sha256':parser_hash,'source_sha256':document.file.sha256,'derivatives':derivatives})
    return anchors,pages,parser_hash


def split_page_anchor(store,anchor):
    """Deterministic source-pixel subdivision; retains original-page geometry and bytes."""
    import io
    from PIL import Image
    if anchor.locator.kind!='page' or anchor.rendering is None:raise ValueError('Cannot geometrically subdivide this source anchor')
    with Image.open(io.BytesIO(store.read(anchor.rendering))) as image:
        w,h=image.size;middle=h//2
        if middle==0:raise ValueError('Source image cannot be split further')
        x0,y0,x1,y1=anchor.locator.bbox;ym=y0+(y1-y0)*middle/h
        result=[]
        for box,bbox in [((0,0,w,middle),(x0,y0,x1,ym)),((0,middle,w,h),(x0,ym,x1,y1))]:
            handle=io.BytesIO();image.crop(box).save(handle,format='PNG');data=handle.getvalue()
            ref=store.put_named('source-images/'+sha(data)+'.png',data,'image/png')
            result.append(SourceAnchor(anchor_id='region-'+sha(canonical([anchor.source_sha256,anchor.locator.page_index,bbox,sha(data)])),
                document_uid=anchor.document_uid,source_sha256=anchor.source_sha256,
                locator=PageLocator(kind='page',page_index=anchor.locator.page_index,bbox=bbox),exact_text=None,rendering=ref))
    return result


def crop_anchor(store,anchor,bbox):
    """Actual pixel crop with its original-page normalized locator, never an inferred transcription."""
    import io,math
    from PIL import Image
    if anchor.locator.kind!='page' or anchor.rendering is None:raise ValueError('Page rendering required')
    a,b,c,d=anchor.locator.bbox;x0,y0,x1,y1=bbox
    if not (a<=x0<x1<=c and b<=y0<y1<=d):raise ValueError('Source crop escapes captured page scope')
    with Image.open(io.BytesIO(store.read(anchor.rendering))) as image:
        w,h=image.size
        box=(max(0,math.floor((x0-a)/(c-a)*w)),max(0,math.floor((y0-b)/(d-b)*h)),
             min(w,math.ceil((x1-a)/(c-a)*w)),min(h,math.ceil((y1-b)/(d-b)*h)))
        output=io.BytesIO();image.crop(box).save(output,format='PNG');data=output.getvalue()
    ref=store.put_named('source-images/'+sha(data)+'.png',data,'image/png')
    return SourceAnchor(anchor_id='region-'+sha(canonical([anchor.source_sha256,anchor.locator.page_index,bbox,sha(data)])),
        document_uid=anchor.document_uid,source_sha256=anchor.source_sha256,locator=PageLocator(kind='page',page_index=anchor.locator.page_index,bbox=bbox),exact_text=None,rendering=ref)
