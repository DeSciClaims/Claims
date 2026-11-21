"""Exact HMAC sampling from guide §§4.1–4.3; never includes pipeline identity."""

from __future__ import annotations
import hashlib
import hmac
from collections import Counter
from fractions import Fraction

PROTOCOL = "claims-source-task-v1"
STREAMS = {
    "C_PROSE",
    "C_TABLE",
    "C_BLOCK",
    "T_TABLE",
    "T_BLOCK",
    "P_BUCKET",
    "PASS_AUDIT",
}


def frame(*parts: str) -> bytes:
    return b"".join(
        len(p.encode("utf-8")).to_bytes(8, "big") + p.encode("utf-8") for p in parts
    )


def rank(secret, scope, stream, item_id):
    if (
        len(secret) != 32
        or len(scope) != 4
        or scope[0] != PROTOCOL
        or stream not in STREAMS
    ):
        raise ValueError("Invalid sampling identity")
    return hmac.new(secret, frame(*scope, stream, item_id), hashlib.sha256).digest()


def ordered(ids, secret, scope, stream):
    ids = list(ids)
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate sampling item")
    return sorted(
        ids, key=lambda x: (rank(secret, scope, stream, x), x.encode("utf-8"))
    )


def seed_commitment(secret, scope, source_sha256, policy_sha256):
    if len(secret) != 32:
        raise ValueError("Paper secret must be 32 bytes")
    return hashlib.sha256(
        frame(*scope, source_sha256, policy_sha256) + secret
    ).hexdigest()


def selected_for_pass_audit(secret, scope, fingerprint):
    return (
        int.from_bytes(rank(secret, scope, "PASS_AUDIT", fingerprint), "big") * 10
        < 2**256
    )


def table_draw(population, count, secret, scope, table_stream, block_stream):
    """Hierarchical draw with source-table round robin and exact inclusion diagnostics."""
    tables = ordered(
        (k for k, v in population.items() if v), secret, scope, table_stream
    )
    selected_tables = tables[:count]
    blocks = {
        t: ordered(population[t], secret, scope, block_stream) for t in selected_tables
    }
    result = []
    for row in range(max((len(v) for v in blocks.values()), default=0)):
        for table in selected_tables:
            if len(result) == count:
                break
            if row < len(blocks[table]):
                result.append(blocks[table][row])
        if len(result) == count:
            break
    draws = Counter(t for t in selected_tables for b in result if b in population[t])
    return result, {
        t: {
            "table_inclusion": Fraction(min(count, len(tables)), len(tables)),
            "conditional_block_inclusion": Fraction(draws[t], len(population[t])),
        }
        for t in selected_tables
    }


def samples(tiles, secret, scope):
    prose = [
        t.tile_id
        for t in tiles
        if t.stratum == "PROSE_OR_EQUATION" and t.eligible_content
    ]
    content_tables, archive_tables = {}, {}
    for tile in tiles:
        if tile.stratum == "TABLE":
            archive_tables.setdefault(tile.table_uid, []).append(tile.tile_id)
            if tile.eligible_content:
                content_tables.setdefault(tile.table_uid, []).append(tile.tile_id)
    nt = sum(map(len, content_tables.values()))
    p, t = (3, 3) if prose and nt else ((6, 0) if prose else (0, 6))
    p, t = min(p, len(prose)), min(t, nt)
    for _ in range(6 - p - t):
        if p < len(prose):
            p += 1
        elif t < nt:
            t += 1
        else:
            break
    cp = ordered(prose, secret, scope, "C_PROSE")[:p]
    ct, cd = table_draw(content_tables, t, secret, scope, "C_TABLE", "C_BLOCK")
    archive, ad = table_draw(archive_tables, 4, secret, scope, "T_TABLE", "T_BLOCK")
    return {
        "content_prose": cp,
        "content_table": ct,
        "archive": archive,
        "content_diagnostics": cd,
        "archive_diagnostics": ad,
    }


def audit_buckets(occupied, secret, scope):
    source = [k for k in occupied if k != "UNANCHORED"]
    return ordered(source, secret, scope, "P_BUCKET")[:4] + (
        ["UNANCHORED"] if "UNANCHORED" in occupied else []
    )


def selection_manifest(store,tiles,secret,scope,name,selected,*,alias=None,population=None):
    """Retain every ranked population and hierarchical draw, including P occupancy."""
    from .contracts import SampleSelection,SelectionTrace,SelectionTraceStep,RankedItem
    specs={'content_prose':('CONTENT_PROSE','C_PROSE','PROSE_TILE'),
           'content_table':('CONTENT_TABLE','C_TABLE','SOURCE_TABLE'),
           'archive':('ARCHIVE','T_TABLE','SOURCE_TABLE'),
           'output_audit':('OUTPUT_AUDIT','P_BUCKET','OUTPUT_BUCKET')}
    stream,domain,level=specs[name];steps=[]
    if population is None:
        population=[t.tile_id for t in tiles if (t.stratum=='PROSE_OR_EQUATION' if name=='content_prose' else t.stratum=='TABLE') and (name=='archive' or t.eligible_content)]
    def step(ids,chosen,seed,kind,parent=None):
        ranked=ordered(ids,secret,scope,seed)
        steps.append(SelectionTraceStep(level=kind,draw_index=len(steps),parent_id=parent,
             conditional_population_ids=sorted(ids,key=lambda x:x.encode()),ranked_items=[RankedItem(item_id=k,rank_sha256=rank(secret,scope,seed,k).hex()) for k in ranked],requested_count=len(chosen),selected_ids=chosen))
    if name in {'content_table','archive'}:
        table_by_tile={t.tile_id:t.table_uid for t in tiles}
        tables=sorted({table_by_tile[k] for k in population})
        chosen_tables=list(dict.fromkeys(table_by_tile[k] for k in selected))
        step(tables,chosen_tables,domain,'SOURCE_TABLE')
        for table in chosen_tables:
            ids=[k for k in population if table_by_tile[k]==table]
            step(ids,[k for k in selected if table_by_tile[k]==table], 'C_BLOCK' if name=='content_table' else 'T_BLOCK','ROW_BLOCK',table)
    else:
        ordinary=[k for k in population if k!='UNANCHORED']
        step(ordinary,[k for k in selected if k!='UNANCHORED'],domain,level)
    trace=store.put(SelectionTrace(stream=stream,seed_domain=domain,steps=steps))
    return SampleSelection(stream=stream,population_ids=sorted(population,key=lambda x:x.encode()),selected_ids=selected,
                           output_alias=alias,seed_domain=domain,selection_trace=trace)
